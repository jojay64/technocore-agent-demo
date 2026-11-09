import copy
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

import technocore_pulse as pulse


class PulseTests(unittest.TestCase):
    def test_did_round_trip(self):
        private_key = ed25519.Ed25519PrivateKey.generate()
        did = pulse.public_key_to_did(private_key.public_key())
        message = b"pulse-test"
        signature = private_key.sign(message)
        pulse.did_to_public_key(did).verify(signature, message)

    def test_report_signature_and_tamper_rejection(self):
        private_key = ed25519.Ed25519PrivateKey.generate()
        did = pulse.public_key_to_did(private_key.public_key())
        report = {
            "schema": "technocore-pulse-report-v1",
            "observer_did": did,
            "generated_at": "2026-09-01T00:00:00+00:00",
            "sample_count": 1,
        }
        signed = pulse.sign_report(report, private_key)
        self.assertTrue(pulse.verify_report(signed))
        tampered = copy.deepcopy(signed)
        tampered["sample_count"] = 2
        with self.assertRaises(ValueError):
            pulse.verify_report(tampered)

    def test_message_analysis_keeps_no_body(self):
        state = pulse.default_state()
        messages = [
            {"seq": 10, "from": "did:key:zExample", "text": "Hello secret"},
            {"seq": 12, "from": "alice", "text": "Hello secret"},
        ]
        metrics = pulse.analyse_messages(messages, state)
        self.assertEqual(metrics["messages_observed"], 2)
        self.assertEqual(metrics["sequence_gaps_observed"], 1)
        self.assertEqual(metrics["exact_duplicate_share"], 0.5)
        serialized = json.dumps(state)
        self.assertNotIn("Hello secret", serialized)

    def test_identity_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            did = pulse.initialize_identity(path)
            _, loaded_did = pulse.load_identity(path)
            self.assertEqual(did, loaded_did)
            with self.assertRaises(FileExistsError):
                pulse.initialize_identity(path)


if __name__ == "__main__":
    unittest.main()
