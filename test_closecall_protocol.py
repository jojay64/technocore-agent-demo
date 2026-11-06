import base64
import json
import socket
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import closecall_protocol as protocol


class CloseCallProtocolTests(unittest.TestCase):
    def setUp(self):
        # Test-only, in-memory identities; never read or write production keys.
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.network_ex = patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden"))
        self.network_ex.start()
        self.addCleanup(self.network_ex.stop)
        self.keys = {}
        self.maker = self.identity()
        self.taker = self.identity()
        self.other = self.identity()

    def identity(self):
        key = Ed25519PrivateKey.generate()
        raw = b"\xed\x01" + key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        value, encoded = int.from_bytes(raw, "big"), ""
        while value:
            value, digit = divmod(value, 58)
            encoded = alphabet[digit] + encoded
        did = "did:key:z" + encoded
        self.keys[did] = key
        return did

    def sign(self, did, text):
        return base64.urlsafe_b64encode(self.keys[did].sign(text.encode())).decode().rstrip("=")

    def record(self, payload, did=None, room="close1", nonce="1790337922535000000"):
        did = did or self.maker
        text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        return {"seq": 10, "ts": "2026-09-25T13:00:00Z", "from": did,
                "nonce": nonce, "text": text, "sig": self.sign(did, f"{room}|{nonce}|{text}")}

    def trade(self, named=False):
        terms = {"id": "trade-1", "maker": self.maker, "px": "226.14", "qty": "0.10",
                 "side": "buy", "taker": self.taker if named else "any", "until": 12}
        text = json.dumps(terms, sort_keys=True, separators=(",", ":"))
        return {"t": "trade", "season": "close-1", "terms": terms, "taker": self.taker,
                "maker_sig": self.sign(self.maker, "close-1|terms|" + text),
                "taker_sig": self.sign(self.taker, "close-1|accept|" + text + "|" + self.taker)}

    def test_unsigned_owner_preparation_uses_historical_public_did(self):
        text = protocol.owner_text()
        self.assertEqual(text, '{"key":"' + protocol.HISTORICAL_DID + '","season":"close-1","t":"owner"}')
        self.assertNotIn("sig", json.loads(text))
        self.assertNotIn("nonce", json.loads(text))

    def test_owner_and_high_precision_integer_nonce_verify(self):
        message = self.record(protocol.owner_text(self.maker))
        message["nonce"] = int(message["nonce"])
        record, payload = protocol.verify_participant_message("close1", message)
        self.assertEqual(record.nonce, "1790337922535000000")
        self.assertEqual(payload["key"], self.maker)

    def test_owner_cannot_register_another_did(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(protocol.owner_text(self.taker)))

    def test_transport_is_bound_to_room_text_nonce_and_sender(self):
        message = self.record(protocol.owner_text(self.maker))
        for field, value in (("text", protocol.owner_text(self.taker)), ("nonce", "2"), ("from", self.taker)):
            with self.subTest(field=field), self.assertRaises(protocol.ProtocolError):
                protocol.verify_transport("close1", {**message, field: value})
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_transport("d-close1-flow", message)

    def test_unlisted_and_referee_rooms_reject_participant_messages(self):
        for room in ("tclk-offers", "d-close1-rules", "other", "d-close1-flow"):
            with self.subTest(room=room), self.assertRaises(protocol.ProtocolError):
                protocol.verify_participant_message(room, self.record(protocol.owner_text(self.maker), room=room))

    def test_invalid_nonce_encodings_are_rejected(self):
        for nonce in (True, 1.5, 0, "0", "01", "-1", "1e3", "9" * 20, None):
            with self.subTest(nonce=nonce), self.assertRaises(protocol.ProtocolError):
                protocol.verify_transport("close1", self.record(protocol.owner_text(self.maker), nonce=nonce))

    def test_signature_encoding_must_be_canonical(self):
        message = self.record(protocol.owner_text(self.maker))
        for signature in (None, message["sig"] + "==", message["sig"][:-1] + "B"):
            with self.subTest(signature=signature), self.assertRaises(protocol.ProtocolError):
                protocol.verify_transport("close1", {**message, "sig": signature})

    def test_server_cleaning_must_not_change_signed_text(self):
        for text in (" hello", "hello ", "hello\nworld", "hello\u200bworld", "hello\ue000world", "hello\u2028world"):
            with self.subTest(text=text), self.assertRaises(protocol.ProtocolError):
                protocol.verify_transport("close1", self.record(text))

    def test_invalid_server_metadata_rejected_but_not_authenticated(self):
        message = self.record(protocol.owner_text(self.maker))
        for field, value in (("seq", True), ("seq", 0), ("ts", "2026-09-25"), ("ts", "bad")):
            with self.subTest(field=field, value=value), self.assertRaises(protocol.ProtocolError):
                protocol.verify_transport("close1", {**message, field: value})
        # Server metadata is NOT signed: a different valid sequence still verifies.
        self.assertEqual(protocol.verify_transport("close1", {**message, "seq": 99}).seq, 99)

    def test_duplicate_keys_and_nonfinite_numbers_rejected(self):
        for text in ('{"a":1,"a":2}', '{"x":{"a":1,"a":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(text=text), self.assertRaises(protocol.ProtocolError):
                protocol.strict_json(text)

    def test_bounded_and_deep_json_rejected(self):
        for text in (" " * 4097, "[" * 1500 + "]" * 1500, '"\ud800"'):
            with self.assertRaises(protocol.ProtocolError):
                protocol.strict_json(text)

    def test_wrong_season_unknown_fields_and_noncompact_owner_rejected(self):
        owner = json.loads(protocol.owner_text(self.maker))
        for payload in ({**owner, "season": "close-2"}, {**owner, "asset": "FLOP"},
                        {**owner, "t": "payment"}, json.dumps(owner)):
            with self.subTest(payload=payload), self.assertRaises(protocol.ProtocolError):
                protocol.verify_participant_message("close1", self.record(payload))

    def test_named_and_open_trades_verify_for_either_publisher(self):
        for named in (True, False):
            for publisher in (self.maker, self.taker):
                payload = self.trade(named)
                _, actual = protocol.verify_participant_message("close1", self.record(payload, did=publisher))
                self.assertEqual(actual, payload)

    def test_maker_signature_domain_and_term_tampering_rejected(self):
        payload = self.trade()
        payload["terms"]["px"] = "226.15"
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(payload))
        payload = self.trade()
        text = protocol.canonical_terms(payload["terms"])
        payload["maker_sig"] = self.sign(self.maker, "close-2|terms|" + text)
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(payload))

    def test_countersignature_binds_actual_taker(self):
        payload = self.trade()
        payload["taker"] = self.other
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(payload))

    def test_named_taker_and_publisher_are_enforced(self):
        payload = self.trade(named=True)
        payload["taker"] = self.other
        text = protocol.canonical_terms(payload["terms"])
        payload["taker_sig"] = self.sign(self.other, "close-1|accept|" + text + "|" + self.other)
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(payload))
        with self.assertRaises(protocol.ProtocolError):
            protocol.verify_participant_message("close1", self.record(self.trade(), did=self.other))

    def test_both_inner_signatures_required_even_with_valid_transport(self):
        for field in ("maker_sig", "taker_sig"):
            payload = self.trade()
            del payload[field]
            with self.subTest(field=field), self.assertRaises(protocol.ProtocolError):
                protocol.verify_participant_message("close1", self.record(payload))

    def test_terms_reject_real_asset_extensions_and_bad_numeric_shapes(self):
        terms = self.trade()["terms"]
        variants = [("asset", "FLOP"), ("qty", 1), ("qty", "0.09"), ("px", "1e2"),
                    ("px", "0"), ("px", "1.001"), ("qty", "NaN"), ("until", True),
                    ("until", 1.0), ("until", 0), ("side", "BUY"), ("id", "a|b")]
        for field, value in variants:
            with self.subTest(field=field, value=value), self.assertRaises(protocol.ProtocolError):
                protocol.canonical_terms({**terms, field: value})

    def test_structurally_valid_expired_trade_is_not_settlement_validation(self):
        # No current sweep/ledger exists in this module; callers must not infer acceptance.
        terms = self.trade()["terms"]
        terms["until"] = 1
        self.assertIn('"until":1', protocol.canonical_terms(terms))

    def test_observed_public_seed_verifies_without_private_key(self):
        message = self.seed_fixture()
        record, payload = protocol.verify_seed(message)
        self.assertEqual(record.did, protocol.OBSERVED_REFEREE_DID)
        self.assertEqual(payload["package"], protocol.MANIFEST_SHA256)

    def test_self_signed_impostor_seed_rejected(self):
        payload = json.loads(self.seed_fixture()["text"])
        message = self.record(payload, room="d-close1-price", nonce="1790337922535")
        message["seq"] = 1
        with self.assertRaisesRegex(protocol.ProtocolError, "unexpected seed signer"):
            protocol.verify_seed(message)

    def test_seed_package_rooms_and_observation_pins_fail_closed(self):
        seed = json.loads(self.seed_fixture()["text"])
        variants = [{**seed, "package": "0" * 64}, {**seed, "rooms": ["evil"]},
                    {**seed, "rooms": [[], *list(protocol.REFEREE_ROOMS)[:4]]},
                    {**seed, "price": "226.15"}, {**seed, "for": 2}]
        # Fixture signer replaces the observed DID only inside this isolated test.
        with patch.object(protocol, "OBSERVED_REFEREE_DID", self.maker):
            for payload in variants:
                message = self.record(payload, room="d-close1-price", nonce="1790337922535")
                message["seq"] = 1
                with self.subTest(payload=payload), self.assertRaises(protocol.ProtocolError):
                    protocol.verify_seed(message)

    def test_manifest_substitution_rejected(self):
        for raw in (b"{}", b"", "not bytes"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.verify_manifest_bytes(raw)

    @staticmethod
    def seed_fixture():
        # Public, signed record collected on 2026-09-25; not a private identity.
        return {
            "seq": 1, "ts": "2026-09-25T12:05:22.575364Z",
            "from": "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte",
            "nonce": 1790337922535,
            "sig": "j3_asvvwrt67C13PdoA2Q1p0QfO24av1hvkC_2Nc5FJet9dKey97CFuKv1ZW9G7Ki4hxw86K-2dfhIjAjhlsCw",
            "text": '{"for":1,"limits":["214.84","237.44"],"package":"bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa","price":"226.14","rooms":["d-close1-flow","d-close1-state","d-close1-price","d-close1-positions","d-close1-pnl"],"season":"close-1","t":"seed","trade":{"tid":626256716983248,"time":"2026-09-25T11:59:42.666000Z"}}',
        }


if __name__ == "__main__":
    unittest.main()
