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

import tclk_agent as agent
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
