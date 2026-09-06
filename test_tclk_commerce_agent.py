import base64
import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

import tclk_commerce_agent as agent
import tclk_offer_watcher as guard


def base58encode(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + (encoded or "1")


def identity():
    private = ed25519.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, "did:key:z" + base58encode(b"\xed\x01" + public)


def signed_message(private, did, room, frame, seq=1, nonce="12345"):
    line = "tclk1 " + guard.canonical_json(frame)
    signature = private.sign(f"{room}|{nonce}|{line}".encode())
    return {
        "seq": seq,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "from": did,
        "nonce": nonce,
        "sig": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "text": line,
    }


class TclkAgentTests(unittest.TestCase):
    def commerce_fixture(self):
        _, payer = identity()
        now = 1700000000000
        record = {"sender": payer, "timestamp_ms": now, "seq": 82}
        frame = {
            "type": "offer", "from": payer, "role": "payer", "amount": "10",
            "asset": "PAPER", "lock": "hash", "rails": ["paper"],
            "expiresMs": now + 120000,
            "claimByMs": now + 300000,
            "refundAfterMs": now + 480000,
            "job": {"proto": "a2a", "id": "job-1", "context": "inline"},
            "nonce": "12345678",
        }
        frame["id"] = guard.offer_id(frame)
        return record, frame


    def test_safe_inline_task_passes_commerce_screen(self):
        record, frame = self.commerce_fixture()
        allowed, _ = agent.commerce_offer_screen(
            record, frame, "Calculate 17 plus 25 and answer with one integer.", "inline"
        )
        self.assertTrue(allowed)

    def test_empty_rail_list_is_rejected(self):
        record, frame = self.commerce_fixture()
        frame["rails"] = []
        allowed, _ = agent.commerce_offer_screen(
            record, frame, "Calculate 17 plus 25 and answer with one integer.", "inline"
        )
        self.assertFalse(allowed)

    def test_nested_job_reference_is_rejected(self):
        record, frame = self.commerce_fixture()
        allowed, reason = agent.commerce_offer_screen(
            record, frame, "Read the full specification from /kv/other/job-2 and summarize it.", "inline"
        )
        self.assertFalse(allowed)
        self.assertIn("nested reference", reason)

    def test_short_expiry_margin_is_rejected(self):
        record, frame = self.commerce_fixture()
        frame["expiresMs"] = record["timestamp_ms"] + 1000
        allowed, reason = agent.commerce_offer_screen(
            record, frame, "Calculate 17 plus 25 and answer with one integer.", "inline"
        )
        self.assertFalse(allowed)
        self.assertIn("expires too soon", reason)


    def test_owned_accept_is_canonical_and_keeps_secret_out_of_frame(self):
        _, offer = self.commerce_fixture()
        secret = bytes.fromhex("ab" * 32)
        accept, encoded_secret = agent.build_owned_accept(
            offer, secret_bytes=secret, nonce="abcdef1234567890"
        )
        self.assertEqual(accept["from"], agent.EXPECTED_DID)
        self.assertNotIn("secret", accept)
        self.assertEqual(encoded_secret, "0x" + secret.hex())
        self.assertEqual(accept["statement"], "0x" + hashlib.sha256(secret).hexdigest())
        self.assertEqual(accept["contract"], agent.contract_id(offer, accept))

    def test_owned_accept_rejects_non_paper_offer(self):
        _, offer = self.commerce_fixture()
        offer["asset"] = "REAL"
        with self.assertRaisesRegex(ValueError, "PAPER hash-lock only"):
            agent.build_owned_accept(offer)


    def test_first_initialization_sets_commerce_cutover_at_current_head(self):
        state = agent.clean_state()
        messages = [{"seq": 77}, {"seq": 81}]
        with patch.object(agent, "room_messages", return_value=messages), patch.object(
            agent, "save_state"
        ) as saved:
            agent.initialize_head(state)
        self.assertTrue(state["initialized"])
        self.assertEqual(state["room_sequences"][agent.OFFER_ROOM], 81)
        self.assertEqual(state["commerce_start_seq"], 81)
        self.assertEqual(state["owned_contracts"], {})
        saved.assert_called_once_with(state)


    def test_staged_accept_keeps_secret_only_in_private_state(self):
        record, offer = self.commerce_fixture()
        state = agent.clean_state()
        state["commerce_start_seq"] = 81
        private_state = agent.clean_private_state()
        secret = bytes.fromhex("cd" * 32)
        with patch.object(agent.secrets, "token_bytes", return_value=secret), patch.object(
            agent.secrets, "token_hex", return_value="ef" * 16
        ), patch.object(
            agent, "save_private_state"
        ) as private_saved, patch.object(agent, "save_state") as public_saved:
            accept = agent.stage_owned_accept(
                record, offer, "Calculate 17 plus 25.", "inline", state, private_state
            )
        contract = accept["contract"]
        self.assertEqual(state["owned_contracts"][contract]["status"], "accept_staged")
        self.assertNotIn(secret.hex(), json.dumps(state))
        self.assertEqual(private_state["owned_contracts"][contract]["secret"], "0x" + secret.hex())
        private_saved.assert_called_once_with(private_state)
        public_saved.assert_called_once_with(state)

    def test_staging_rejects_offer_at_or_before_cutover(self):
        record, offer = self.commerce_fixture()
        state = agent.clean_state()
        state["commerce_start_seq"] = record["seq"]
        with self.assertRaisesRegex(ValueError, "predates the commerce cutover"):
            agent.stage_owned_accept(
                record, offer, "Calculate 17 plus 25.", "inline",
                state, agent.clean_private_state()
            )

    def test_staging_rejects_second_active_owned_contract(self):
        record, offer = self.commerce_fixture()
        state = agent.clean_state()
        state["commerce_start_seq"] = 81
        state["owned_contracts"]["existing"] = {"status": "locked"}
        with self.assertRaisesRegex(ValueError, "already active"):
            agent.stage_owned_accept(
                record, offer, "Calculate 17 plus 25.", "inline",
                state, agent.clean_private_state()
            )


    def test_outbound_signature_verifies_and_is_bound_to_room(self):
        private, did = identity()
        contract = "0x" + "44" * 32
        room = agent.deal_room(contract)
        frame = {
            "type": "heartbeat", "from": did,
            "contract": contract, "nonce": "abcdef12",
        }
        with patch.object(agent, "EXPECTED_DID", did):
            outbound = agent.sign_outbound_frame(
                private, did, room, frame, "1700000000000000000"
            )
        message = {
            "seq": 1, "ts": datetime.now(timezone.utc).isoformat(),
            "from": did, "nonce": outbound["transport_nonce"],
            "sig": outbound["transport_signature"], "text": outbound["content"],
        }
        record = agent.transport_record(room, message)
        agent.verify_transport(record)
        wrong_room_record = agent.transport_record(agent.OFFER_ROOM, message)
        with self.assertRaisesRegex(ValueError, "does not verify"):
            agent.verify_transport(wrong_room_record)

    def test_outbound_signature_rejects_non_historical_did(self):
        private, did = identity()
        with self.assertRaisesRegex(ValueError, "historical Research DID"):
            agent.sign_outbound_content(private, did, agent.OFFER_ROOM, "test", "12345")


    def test_transport_nonce_is_persistent_and_monotonic(self):
        private_state = agent.clean_private_state()
        private_state["last_transport_nonce"] = 200
        with patch.object(agent.time, "time_ns", return_value=100), patch.object(
            agent, "save_private_state"
        ) as saved:
            nonce = agent.next_transport_nonce(private_state)
        self.assertEqual(nonce, "201")
        self.assertEqual(private_state["last_transport_nonce"], 201)
        saved.assert_called_once_with(private_state)

    def test_invalid_private_transport_nonce_is_rejected(self):
        private_state = agent.clean_private_state()
        private_state["last_transport_nonce"] = "invalid"
        with self.assertRaisesRegex(RuntimeError, "nonce is invalid"):
            agent.next_transport_nonce(private_state)


    def test_historical_signer_reuses_existing_identity(self):
        private, did = identity()
        raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            original = json.dumps({"did": did, "private_key_hex": raw.hex()})
            path.write_text(original, encoding="utf-8")
            with patch.object(agent, "IDENTITY_FILE", path), patch.object(
                agent, "EXPECTED_DID", did
            ):
                signer, loaded_did = agent.load_historical_signer()
            self.assertEqual(loaded_did, did)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            signer.public_key().verify(signer.sign(b"test"), b"test")


    def test_signed_post_returns_verified_server_record(self):
        private, did = identity()
        contract = "0x" + "55" * 32
        room = agent.deal_room(contract)
        frame = {
            "type": "heartbeat", "from": did,
            "contract": contract, "nonce": "abcdef12",
        }
        with patch.object(agent, "EXPECTED_DID", did):
            outbound = agent.sign_outbound_frame(private, did, room, frame, "12345")
            posted = signed_message(private, did, room, frame, seq=9, nonce="12345")
            reply = json.dumps({"posted": posted}).encode("utf-8")
            with patch.object(agent.guard.OPENER, "open") as opened:
                response = opened.return_value.__enter__.return_value
                response.read.return_value = reply
                record = agent.post_signed_content(outbound)
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertNotIn(outbound["transport_signature"], request.full_url)
        self.assertEqual(record["seq"], 9)
        self.assertEqual(record["line"], outbound["content"])


    def test_commerce_requires_explicit_activation(self):
        with patch.object(agent, "COMMERCE_MODE", "DISABLED"):
            with self.assertRaisesRegex(RuntimeError, "commerce is disabled"):
                agent.verify_commerce_activation()

    def test_exact_paper_commerce_mode_is_accepted(self):
        with patch.object(agent, "COMMERCE_MODE", "PAPER_COMMERCE"), patch.object(
            guard, "BASE_URL", "https://technocore.chat"
        ):
            self.assertEqual(agent.verify_commerce_activation(), "PAPER_COMMERCE")


    def test_private_state_is_written_with_mode_600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            value = {"version": 1, "owned_contracts": {"test": {"secret": "hidden"}}}
            with patch.object(agent, "PRIVATE_STATE_FILE", path):
                agent.save_private_state(value)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(agent.load_private_state(), value)

    def test_private_state_rejects_permissive_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(json.dumps(agent.clean_private_state()), encoding="utf-8")
            path.chmod(0o644)
            with patch.object(agent, "PRIVATE_STATE_FILE", path):
                with self.assertRaisesRegex(RuntimeError, "permissions must be 600"):
                    agent.load_private_state()


    def test_complete_transport_record_verifies(self):
        private, did = identity()
        frame = {
            "type": "heartbeat", "from": did,
            "contract": "0x" + "12" * 32, "nonce": "12345678",
        }
        record = agent.transport_record(
            "mb-p-tclk-" + "12" * 8,
            signed_message(private, did, "mb-p-tclk-" + "12" * 8, frame),
        )
        agent.verify_transport(record)
        self.assertEqual(record["line"], "tclk1 " + guard.canonical_json(frame))
        self.assertIn("signature", record)

    def test_signature_is_bound_to_room(self):
        private, did = identity()
        frame = {
            "type": "heartbeat", "from": did,
            "contract": "0x" + "34" * 32, "nonce": "12345678",
        }
        message = signed_message(private, did, agent.OFFER_ROOM, frame)
        record = agent.transport_record("mb-p-tclk-" + "34" * 8, message)
        with self.assertRaisesRegex(ValueError, "does not verify"):
            agent.verify_transport(record)

    def test_heartbeat_keeps_state_unchanged(self):
        payer_key, payer = identity()
        _, payee = identity()
        contract_id = "0x" + "56" * 32
        contract = {
            "contract": contract_id,
            "offer": {"refundAfterMs": int(time.time() * 1000) + 60_000},
            "payer_did": payer,
            "payee_did": payee,
            "statement": "0x" + "00" * 32,
            "status": "accepted",
        }
        frame = {
            "type": "heartbeat", "from": payer,
            "contract": contract_id, "nonce": "abcdef12", "note": "working",
        }
        after, valid, _ = agent.apply_contract_frame(contract, frame, int(time.time() * 1000))
        self.assertTrue(valid)
        self.assertEqual(after, "accepted")
        self.assertEqual(contract["status"], "accepted")
        self.assertIsNotNone(payer_key)

    def test_non_party_heartbeat_is_rejected(self):
        _, payer = identity()
        _, payee = identity()
        _, stranger = identity()
        contract_id = "0x" + "78" * 32
        contract = {
            "contract": contract_id,
            "offer": {"refundAfterMs": int(time.time() * 1000) + 60_000},
            "payer_did": payer, "payee_did": payee,
            "statement": "0x" + "00" * 32, "status": "locked",
        }
        frame = {
            "type": "heartbeat", "from": stranger,
            "contract": contract_id, "nonce": "abcdef12",
        }
        after, valid, reason = agent.apply_contract_frame(contract, frame, int(time.time() * 1000))
        self.assertFalse(valid)
        self.assertEqual(after, "locked")
        self.assertIn("not a party", reason)

    def test_contract_id_matches_accept_core(self):
        _, payer = identity()
        _, payee = identity()
        now = int(time.time() * 1000)
        fields = {
            "type": "offer", "from": payer, "role": "payer", "amount": "1",
            "asset": "PAPER", "lock": "hash", "rails": ["paper"],
            "claimByMs": now + 120_000, "refundAfterMs": now + 180_000,
            "expiresMs": now + 60_000, "nonce": "12345678",
        }
        offer = {**fields, "id": guard.offer_id(fields)}
        secret = bytes.fromhex("ab" * 32)
        accept = {
            "type": "accept", "from": payee, "ref": offer["id"],
            "statement": "0x" + hashlib.sha256(secret).hexdigest(),
            "nonce": "abcdef12",
        }
        first = agent.contract_id(offer, accept)
        accept["contract"] = first
        self.assertEqual(agent.contract_id(offer, accept), first)
        self.assertEqual(agent.deal_room(first), "mb-p-tclk-" + first[2:18])

    def test_transcript_entry_keeps_signed_content(self):
        private, did = identity()
        frame = {
            "type": "heartbeat", "from": did,
            "contract": "0x" + "90" * 32, "nonce": "12345678",
        }
        room = "mb-p-tclk-" + "90" * 8
        record = agent.transport_record(room, signed_message(private, did, room, frame))
        entry = agent.transcript_entry(record, frame, "accepted", "accepted", True)
        self.assertEqual(entry["room"], room)
        self.assertEqual(entry["sender_did"], did)
        self.assertEqual(entry["content"], record["line"])
        self.assertEqual(entry["frame_type"], "heartbeat")
        self.assertEqual(entry["state_before"], entry["state_after"])

    def test_identity_mismatch_fails_without_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            path.write_text(json.dumps({"did": "wrong", "private_key_hex": "00" * 32}))
            with patch.object(agent, "IDENTITY_FILE", path):
                with self.assertRaisesRegex(RuntimeError, "DID mismatch"):
                    agent.verify_historical_identity()

    def test_matching_historical_identity_loads_without_rewrite(self):
        private, did = identity()
        raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            original = json.dumps({"did": did, "private_key_hex": raw.hex()})
            path.write_text(original, encoding="utf-8")
            with patch.object(agent, "IDENTITY_FILE", path), patch.object(
                agent, "EXPECTED_DID", did
            ):
                self.assertEqual(agent.verify_historical_identity(), did)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
