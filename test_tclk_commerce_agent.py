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


    def test_daily_accept_limit_is_fail_closed(self):
        record, offer = self.commerce_fixture()
        state = agent.clean_state()
        state["commerce_start_seq"] = 81
        now = 1700000000.0
        state["accept_history"] = [
            {"staged_at": now - 10},
            {"staged_at": now - 20},
            {"staged_at": now - 30},
        ]
        with patch.object(agent.time, "time", return_value=now):
            with self.assertRaisesRegex(ValueError, "daily PAPER accept limit"):
                agent.stage_owned_accept(
                    record, offer, "Calculate 17 plus 25.", "inline",
                    state, agent.clean_private_state()
                )
        state["accept_history"] = [{"staged_at": now - agent.ACCEPT_WINDOW_SECONDS - 1}]
        self.assertEqual(agent.recent_accept_count(state, now=now), 0)


    def staged_contract_fixture(self):
        private, did = identity()
        record, offer = self.commerce_fixture()
        state = agent.clean_state()
        state["commerce_start_seq"] = 81
        private_state = agent.clean_private_state()
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.secrets, "token_bytes", return_value=bytes.fromhex("aa" * 32)
        ), patch.object(agent.secrets, "token_hex", return_value="bb" * 16), patch.object(
            agent, "save_private_state"
        ), patch.object(agent, "save_state"):
            accept = agent.stage_owned_accept(
                record, offer, "Calculate 17 plus 25.", "inline", state, private_state
            )
        return private, did, state, private_state, accept["contract"]


    def test_publish_staged_accept_records_verified_success(self):
        private, did, state, private_state, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        message = signed_message(
            private, did, agent.OFFER_ROOM, owned["accept"], seq=99, nonce="12345"
        )
        record = agent.transport_record(agent.OFFER_ROOM, message)
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(agent, "post_signed_content", return_value=record) as posted, patch.object(
            agent, "append_jsonl"
        ) as transcript, patch.object(agent, "save_state"):
            returned = agent.publish_staged_accept(
                contract, state, private_state, private, did
            )
        self.assertEqual(returned["seq"], 99)
        self.assertEqual(owned["status"], "accepted")
        self.assertEqual(owned["accept_seq"], 99)
        posted.assert_called_once()
        transcript.assert_called_once()


    def test_publish_failure_becomes_uncertain_without_retry(self):
        private, did, state, private_state, contract = self.staged_contract_fixture()
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(
            agent, "post_signed_content", side_effect=TimeoutError("simulated timeout")
        ) as posted, patch.object(agent, "append_jsonl") as decision_log, patch.object(
            agent, "save_state"
        ):
            with self.assertRaises(TimeoutError):
                agent.publish_staged_accept(
                    contract, state, private_state, private, did
                )
        self.assertEqual(state["owned_contracts"][contract]["status"], "accept_uncertain")
        self.assertIn("simulated timeout", state["owned_contracts"][contract]["last_error"])
        posted.assert_called_once()
        decision_log.assert_called_once()


    def test_delivery_is_normalized_and_bounded(self):
        self.assertEqual(agent.normalize_delivery("  answer   42  "), "answer 42")
        with self.assertRaisesRegex(ValueError, "character limit"):
            agent.normalize_delivery("x" * (agent.MAX_DELIVERY_CHARS + 1))

    def test_delivery_cannot_impersonate_protocol_or_include_url(self):
        with self.assertRaisesRegex(ValueError, "impersonate"):
            agent.normalize_delivery("tclk1 {}")
        with self.assertRaisesRegex(ValueError, "external URL"):
            agent.normalize_delivery("See https://example.com for the result")


    def locked_contract_fixture(self):
        private, did, state, private_state, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "locked"
        owned["rail_verified"] = True
        owned["rail_ref"] = contract
        return private, did, state, private_state, contract


    def test_locked_contract_runs_full_pipeline_to_ready_delivery(self):
        _, did, state, _, contract = self.locked_contract_fixture()
        research = {"decision": "COMPLETE", "answer": "42", "reason": "calculated"}
        critic = {"decision": "APPROVE", "reason": "correct"}
        judge = {"decision": "APPROVE", "reason": "validated"}
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.time, "time", return_value=1700000000.0
        ), patch.object(agent, "research_execute_task", return_value=research) as research_call, patch.object(
            agent, "critic_verify_delivery", return_value=critic
        ) as critic_call, patch.object(
            agent, "judge_validate_delivery", return_value=judge
        ) as judge_call, patch.object(agent, "append_jsonl"), patch.object(agent, "save_state"):
            status = agent.run_owned_pipeline(contract, state)
        owned = state["owned_contracts"][contract]
        self.assertEqual(status, "ready_to_deliver")
        self.assertEqual(owned["delivery_text"], "42")
        self.assertEqual(owned["pipeline"]["judge"]["decision"], "APPROVE")
        research_call.assert_called_once()
        critic_call.assert_called_once()
        judge_call.assert_called_once()


    def test_rejected_pipeline_never_creates_delivery(self):
        _, did, state, _, contract = self.locked_contract_fixture()
        research = {"decision": "COMPLETE", "answer": "42", "reason": "calculated"}
        critic = {"decision": "REJECT", "reason": "incorrect"}
        judge = {"decision": "REJECT", "reason": "prerequisites rejected"}
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.time, "time", return_value=1700000000.0
        ), patch.object(agent, "research_execute_task", return_value=research), patch.object(
            agent, "critic_verify_delivery", return_value=critic
        ), patch.object(agent, "judge_validate_delivery", return_value=judge), patch.object(
            agent, "append_jsonl"
        ), patch.object(agent, "save_state"):
            status = agent.run_owned_pipeline(contract, state)
        owned = state["owned_contracts"][contract]
        self.assertEqual(status, "execution_rejected")
        self.assertNotIn("delivery_text", owned)

    def test_unverified_lock_never_runs_research(self):
        _, did, state, _, contract = self.locked_contract_fixture()
        state["owned_contracts"][contract]["rail_verified"] = False
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "research_execute_task"
        ) as research_call:
            with self.assertRaisesRegex(ValueError, "lock is not verified"):
                agent.run_owned_pipeline(contract, state)
        research_call.assert_not_called()


    def test_owned_lock_requires_exact_paper_rail_note(self):
        _, _, state, _, contract = self.locked_contract_fixture()
        owned = state["owned_contracts"][contract]
        frame = {
            "type": "lock", "from": owned["payer_did"],
            "contract": contract, "rail": "paper", "ref": contract,
        }
        expected = (
            f"tclkpaper1 locked hash {owned["statement"]} "
            f"{owned["offer"]["refundAfterMs"]}"
        )
        with patch.object(agent.guard, "read_url", return_value=expected) as read:
            evidence = agent.verify_owned_paper_lock(owned, frame)
        namespace, key = agent.paper_note_location(contract)
        self.assertEqual(evidence["namespace"], namespace)
        self.assertEqual(evidence["key"], key)
        self.assertIn(f"/kv/{namespace}/{key}", read.call_args.args[0])

    def test_owned_lock_rejects_wrong_ref_or_tampered_note(self):
        _, _, state, _, contract = self.locked_contract_fixture()
        owned = state["owned_contracts"][contract]
        frame = {
            "type": "lock", "from": owned["payer_did"],
            "contract": contract, "rail": "paper", "ref": "wrong",
        }
        with self.assertRaisesRegex(ValueError, "reference must equal"):
            agent.verify_owned_paper_lock(owned, frame)
        frame["ref"] = contract
        with patch.object(agent.guard, "read_url", return_value="tampered"):
            with self.assertRaisesRegex(ValueError, "does not match"):
                agent.verify_owned_paper_lock(owned, frame)


    def test_delivery_artifact_is_deterministic_and_contains_no_secret(self):
        _, did, state, private_state, contract = self.locked_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "ready_to_deliver"
        owned["delivery_text"] = "42"
        owned["pipeline"] = {
            "research": {"decision": "COMPLETE", "answer": "42", "reason": "done"},
            "critic": {"decision": "APPROVE", "reason": "correct"},
            "judge": {"decision": "APPROVE", "reason": "valid"},
        }
        with patch.object(agent, "EXPECTED_DID", did):
            first, content = agent.build_delivery_artifact(owned)
            second, repeated = agent.build_delivery_artifact(owned)
        secret = private_state["owned_contracts"][contract]["secret"]
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(content, repeated)
        self.assertTrue(content.startswith("tclk-delivery1 "))
        self.assertNotIn(secret, content)
        self.assertEqual(first["result_sha256"], hashlib.sha256(b"42").hexdigest())


    def ready_delivery_fixture(self):
        private, did, state, private_state, contract = self.locked_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "ready_to_deliver"
        owned["delivery_text"] = "42"
        owned["delivery_sha256"] = hashlib.sha256(b"42").hexdigest()
        owned["pipeline"] = {
            "research": {"decision": "COMPLETE", "answer": "42", "reason": "done"},
            "critic": {"decision": "APPROVE", "reason": "correct"},
            "judge": {"decision": "APPROVE", "reason": "valid"},
        }
        return private, did, state, private_state, contract


    def test_delivery_publication_is_recorded_before_reveal(self):
        private, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        def fake_post(outbound):
            return {
                "room": outbound["room"], "seq": 7,
                "ts": "2026-09-06T12:00:00Z", "timestamp_ms": 1788696000000,
                "sender": outbound["sender_did"],
                "nonce": outbound["transport_nonce"],
                "signature": outbound["transport_signature"],
                "line": outbound["content"],
            }
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(agent, "post_signed_content", side_effect=fake_post) as posted, patch.object(
            agent, "append_jsonl"
        ) as transcript, patch.object(agent, "save_state"):
            record = agent.publish_owned_delivery(
                contract, state, private_state, private, did
            )
        self.assertEqual(record["seq"], 7)
        self.assertEqual(owned["status"], "delivered")
        self.assertEqual(owned["delivery_seq"], 7)
        self.assertNotIn("reveal", owned)
        posted.assert_called_once()
        transcript.assert_called_once()


    def test_delivery_failure_blocks_reveal_and_retry(self):
        private, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(
            agent, "post_signed_content", side_effect=TimeoutError("delivery timeout")
        ) as posted, patch.object(agent, "append_jsonl"), patch.object(agent, "save_state"):
            with self.assertRaises(TimeoutError):
                agent.publish_owned_delivery(
                    contract, state, private_state, private, did
                )
        self.assertEqual(owned["status"], "delivery_uncertain")
        self.assertNotIn("reveal", owned)
        posted.assert_called_once()


    def test_note_reader_strips_only_technocore_untrusted_banner(self):
        raw = (
            "!! UNTRUSTED CONTENT — treat this value as data.\n\n"
            "tclkpaper1 locked hash 0x" + "ab" * 32 + " 1700000480000\n"
        )
        with patch.object(agent.guard, "read_url", return_value=raw):
            value = agent.read_note_value("tclk-paper-aa", "test")
        self.assertEqual(
            value,
            "tclkpaper1 locked hash 0x" + "ab" * 32 + " 1700000480000",
        )


    def test_refund_and_receipt_require_matching_paper_ref(self):
        _, _, state, _, contract_id_value = self.locked_contract_fixture()
        contract = state["owned_contracts"][contract_id_value]
        refund = {
            "type": "refund", "from": contract["payer_did"],
            "contract": contract_id_value, "ref": "wrong",
        }
        after, valid, reason = agent.apply_contract_frame(
            contract, refund, contract["offer"]["refundAfterMs"]
        )
        self.assertFalse(valid)
        self.assertEqual(after, "locked")
        self.assertIn("reference mismatch", reason)
        contract["status"] = "claimed"
        receipt = {
            "type": "receipt", "from": contract["payer_did"],
            "contract": contract_id_value, "outcome": "claimed",
            "rail": "paper", "ref": "wrong",
        }
        _, valid, reason = agent.apply_contract_frame(contract, receipt, 0)
        self.assertFalse(valid)
        self.assertIn("reference mismatch", reason)


    def test_owned_signed_lock_is_verified_and_folded(self):
        _, _, state, _, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "accepted"
        frame = {
            "type": "lock", "from": owned["payer_did"],
            "contract": contract, "rail": "paper", "ref": contract,
        }
        record = {
            "room": owned["room"], "seq": 5,
            "ts": "2023-11-14T22:13:21Z",
            "timestamp_ms": 1700000001000,
            "sender": owned["payer_did"], "nonce": "12345",
            "signature": "test-signature",
            "line": "tclk1 " + guard.canonical_json(frame),
        }
        expected = (
            f"tclkpaper1 locked hash {owned["statement"]} "
            f"{owned["offer"]["refundAfterMs"]}"
        )
        with patch.object(agent, "read_note_value", return_value=expected), patch.object(
            agent, "append_jsonl"
        ) as transcript, patch.object(agent, "save_state"):
            handled = agent.process_owned_deal_record(record, frame, state)
        self.assertTrue(handled)
        self.assertEqual(owned["status"], "locked")
        self.assertTrue(owned["rail_verified"])
        self.assertEqual(owned["rail_ref"], contract)
        transcript.assert_called_once()


    def test_reveal_requires_prior_delivery_and_matching_private_secret(self):
        _, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        with patch.object(agent, "EXPECTED_DID", did):
            with self.assertRaisesRegex(ValueError, "delivery must precede"):
                agent.build_owned_reveal(owned, private_state, now_ms=1700000001000)
            owned["status"] = "delivered"
            owned["delivery_seq"] = 7
            reveal = agent.build_owned_reveal(
                owned, private_state, now_ms=1700000001000
            )
        self.assertEqual(reveal["from"], did)
        self.assertEqual(reveal["contract"], contract)
        self.assertEqual(reveal["ref"], contract)
        digest = "0x" + hashlib.sha256(bytes.fromhex(reveal["secret"][2:])).hexdigest()
        self.assertEqual(digest, owned["statement"])

    def test_reveal_refuses_expired_claim_deadline(self):
        _, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "delivered"
        owned["delivery_seq"] = 7
        with patch.object(agent, "EXPECTED_DID", did):
            with self.assertRaisesRegex(ValueError, "claim deadline passed"):
                agent.build_owned_reveal(
                    owned, private_state, now_ms=owned["offer"]["claimByMs"]
                )


    def test_paper_note_cas_handles_success_and_conflict(self):
        locked = "tclkpaper1 locked hash 0x" + "ab" * 32 + " 1700000480000"
        claimed = locked.replace(" locked ", " claimed ") + " 0x" + "cd" * 32
        with patch.object(agent.guard.OPENER, "open") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b"ok"
            self.assertTrue(agent.set_note_cas("tclk-paper-aa", "contractkey", claimed, locked))
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIn("if=", request.full_url)
        conflict = agent.urllib.error.HTTPError(
            "https://technocore.chat", 409, "conflict", None, None
        )
        with patch.object(agent.guard.OPENER, "open", side_effect=conflict):
            self.assertFalse(
                agent.set_note_cas("tclk-paper-aa", "contractkey", claimed, locked)
            )


    def test_paper_claim_is_after_reveal_and_idempotent(self):
        _, _, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "reveal_published"
        owned["reveal_seq"] = 8
        secret = private_state["owned_contracts"][contract]["secret"]
        locked = (
            f"tclkpaper1 locked hash {owned["statement"]} "
            f"{owned["offer"]["refundAfterMs"]}"
        )
        claimed = locked.replace(" locked ", " claimed ", 1) + " " + secret
        with patch.object(agent, "read_note_value", return_value=locked), patch.object(
            agent, "set_note_cas", return_value=True
        ) as cas:
            evidence = agent.claim_owned_paper_note(
                owned, private_state, now_ms=1700000001000
            )
        self.assertIn("value_sha256", evidence)
        cas.assert_called_once()
        with patch.object(agent, "read_note_value", return_value=claimed), patch.object(
            agent, "set_note_cas"
        ) as repeated_cas:
            repeated = agent.claim_owned_paper_note(
                owned, private_state, now_ms=1700000001000
            )
        self.assertEqual(evidence, repeated)
        repeated_cas.assert_not_called()


    def test_reveal_is_published_before_paper_claim(self):
        private, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "delivered"
        owned["delivery_seq"] = 7
        order = []
        def verified(*args):
            order.append("lock_verified")
            return {"value_sha256": "lock-hash"}
        def posted(outbound):
            order.append("reveal_published")
            return {
                "room": outbound["room"], "seq": 8,
                "ts": "2023-11-14T22:13:22Z", "timestamp_ms": 1700000002000,
                "sender": outbound["sender_did"],
                "nonce": outbound["transport_nonce"],
                "signature": outbound["transport_signature"],
                "line": outbound["content"],
            }
        def claimed(*args):
            order.append("paper_claimed")
            return {"value_sha256": "claim-hash"}
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.time, "time", return_value=1700000000.0
        ), patch.object(agent, "verify_owned_paper_lock", side_effect=verified), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(agent, "post_signed_content", side_effect=posted), patch.object(
            agent, "claim_owned_paper_note", side_effect=claimed
        ), patch.object(agent, "append_jsonl"), patch.object(agent, "save_state"):
            record = agent.publish_reveal_and_claim(
                contract, state, private_state, private, did
            )
        self.assertEqual(order, ["lock_verified", "reveal_published", "paper_claimed"])
        self.assertEqual(record["seq"], 8)
        self.assertEqual(owned["status"], "claimed")
        self.assertEqual(owned["reveal_seq"], 8)


    def test_uncertain_reveal_never_attempts_paper_claim(self):
        private, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "delivered"
        owned["delivery_seq"] = 7
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.time, "time", return_value=1700000000.0
        ), patch.object(
            agent, "verify_owned_paper_lock", return_value={"value_sha256": "lock"}
        ), patch.object(agent, "next_transport_nonce", return_value="12345"), patch.object(
            agent, "post_signed_content", side_effect=TimeoutError("reveal timeout")
        ), patch.object(agent, "claim_owned_paper_note") as claim, patch.object(
            agent, "append_jsonl"
        ), patch.object(agent, "save_state"):
            with self.assertRaises(TimeoutError):
                agent.publish_reveal_and_claim(
                    contract, state, private_state, private, did
                )
        self.assertEqual(owned["status"], "reveal_uncertain")
        self.assertNotIn("reveal_seq", owned)
        claim.assert_not_called()


    def test_claim_failure_preserves_confirmed_reveal(self):
        private, did, state, private_state, contract = self.ready_delivery_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "delivered"
        owned["delivery_seq"] = 7
        def posted(outbound):
            return {
                "room": outbound["room"], "seq": 8,
                "ts": "2023-11-14T22:13:22Z", "timestamp_ms": 1700000002000,
                "sender": outbound["sender_did"],
                "nonce": outbound["transport_nonce"],
                "signature": outbound["transport_signature"],
                "line": outbound["content"],
            }
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent.time, "time", return_value=1700000000.0
        ), patch.object(
            agent, "verify_owned_paper_lock", return_value={"value_sha256": "lock"}
        ), patch.object(agent, "next_transport_nonce", return_value="12345"), patch.object(
            agent, "post_signed_content", side_effect=posted
        ) as reveal_post, patch.object(
            agent, "claim_owned_paper_note", side_effect=TimeoutError("claim timeout")
        ), patch.object(agent, "append_jsonl"), patch.object(agent, "save_state"):
            with self.assertRaises(TimeoutError):
                agent.publish_reveal_and_claim(
                    contract, state, private_state, private, did
                )
        self.assertEqual(owned["status"], "claim_uncertain")
        self.assertEqual(owned["reveal_seq"], 8)
        reveal_post.assert_called_once()


    def test_approved_offer_stages_and_publishes_one_accept(self):
        private, did = identity()
        record, offer = self.commerce_fixture()
        record.update({
            "room": agent.OFFER_ROOM, "ts": "2023-11-14T22:13:20Z",
            "nonce": "12345", "signature": "test-signature",
            "line": "tclk1 " + guard.canonical_json(offer),
        })
        state = agent.clean_state()
        state["commerce_start_seq"] = 81
        runtime = {
            "private_key": private, "did": did,
            "private_state": agent.clean_private_state(),
        }
        approval = {"decision": "APPROVE", "reason": "safe"}
        contract = "0x" + "66" * 32
        staged = {"contract": contract}
        accepted_record = {"seq": 100}
        with patch.object(agent.guard, "resolve_context", return_value=(
            "Calculate 17 plus 25 and answer with one integer.", "inline"
        )), patch.object(agent.guard, "research_review", return_value=approval), patch.object(
            agent.guard, "critic_review", return_value=approval
        ), patch.object(agent.guard, "judge_review", return_value=approval), patch.object(
            agent, "stage_owned_accept", return_value=staged
        ) as stage, patch.object(
            agent, "publish_staged_accept", return_value=accepted_record
        ) as publish, patch.object(agent, "append_jsonl"), patch.object(agent, "save_state"):
            agent.evaluate_offer(record, offer, state, runtime)
        stage.assert_called_once()
        publish.assert_called_once()


    def test_locked_contract_advances_through_delivery_and_claim(self):
        private, did, state, private_state, contract = self.locked_contract_fixture()
        runtime = {"private_key": private, "did": did, "private_state": private_state}
        owned = state["owned_contracts"][contract]
        order = []
        def pipeline(*args):
            order.append("pipeline")
            owned["status"] = "ready_to_deliver"
        def delivery(*args):
            order.append("delivery")
            owned["status"] = "delivered"
        def reveal(*args):
            order.append("reveal_claim")
            owned["status"] = "claimed"
        with patch.object(agent, "run_owned_pipeline", side_effect=pipeline), patch.object(
            agent, "publish_owned_delivery", side_effect=delivery
        ), patch.object(agent, "publish_reveal_and_claim", side_effect=reveal):
            status = agent.advance_owned_contract(contract, state, runtime)
        self.assertEqual(order, ["pipeline", "delivery", "reveal_claim"])
        self.assertEqual(status, "claimed")


    def test_resume_never_retries_uncertain_network_writes(self):
        state = agent.clean_state()
        state["owned_contracts"] = {
            "accept": {"status": "accept_uncertain"},
            "delivery": {"status": "delivery_uncertain"},
            "reveal": {"status": "reveal_uncertain"},
        }
        runtime = {
            "private_key": object(), "did": agent.EXPECTED_DID,
            "private_state": agent.clean_private_state(),
        }
        with patch.object(agent, "publish_staged_accept") as accept, patch.object(
            agent, "advance_owned_contract"
        ) as advance:
            agent.resume_owned_work(state, runtime)
        accept.assert_not_called()
        advance.assert_not_called()


    def test_heartbeat_is_rate_limited_and_state_neutral(self):
        private, did, state, private_state, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "accepted"
        runtime = {"private_key": private, "did": did, "private_state": private_state}
        def posted(outbound):
            return {
                "room": outbound["room"], "seq": 6,
                "ts": "2023-11-14T22:13:21Z", "timestamp_ms": 1700000001000,
                "sender": outbound["sender_did"],
                "nonce": outbound["transport_nonce"],
                "signature": outbound["transport_signature"],
                "line": outbound["content"],
            }
        with patch.object(agent, "EXPECTED_DID", did), patch.object(
            agent, "next_transport_nonce", return_value="12345"
        ), patch.object(agent, "post_signed_content", side_effect=posted) as send, patch.object(
            agent, "append_jsonl"
        ), patch.object(agent, "save_state"):
            agent.maybe_publish_heartbeats(state, runtime, now=1700000000.0)
            agent.maybe_publish_heartbeats(state, runtime, now=1700000010.0)
        self.assertEqual(owned["status"], "accepted")
        self.assertEqual(owned["last_heartbeat_seq"], 6)
        send.assert_called_once()


    def test_waiting_contract_expires_without_late_heartbeat(self):
        private, did, state, private_state, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "accepted"
        runtime = {"private_key": private, "did": did, "private_state": private_state}
        expired_now = owned["offer"]["claimByMs"] / 1000
        with patch.object(agent, "post_signed_content") as send, patch.object(
            agent, "append_jsonl"
        ), patch.object(agent, "save_state"):
            agent.maybe_publish_heartbeats(state, runtime, now=expired_now)
        self.assertEqual(owned["status"], "expired_unlocked")
        send.assert_not_called()

    def test_late_owned_lock_is_rejected_before_note_read(self):
        _, _, state, _, contract = self.staged_contract_fixture()
        owned = state["owned_contracts"][contract]
        owned["status"] = "accepted"
        frame = {
            "type": "lock", "from": owned["payer_did"],
            "contract": contract, "rail": "paper", "ref": contract,
        }
        record = {
            "room": owned["room"], "seq": 5,
            "ts": "2023-11-14T22:18:20Z",
            "timestamp_ms": owned["offer"]["claimByMs"],
            "sender": owned["payer_did"], "nonce": "12345",
            "signature": "test-signature",
            "line": "tclk1 " + guard.canonical_json(frame),
        }
        with patch.object(agent, "read_note_value") as note_read, patch.object(
            agent, "append_jsonl"
        ), patch.object(agent, "save_state"):
            agent.process_owned_deal_record(record, frame, state)
        self.assertEqual(owned["status"], "accepted")
        note_read.assert_not_called()


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
