import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import closecall_observer as observer
import closecall_protocol as protocol


class CloseCallObserverTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.no_connect_ex = patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden"))
        self.no_connect_ex.start()
        self.addCleanup(self.no_connect_ex.stop)
        self.keys = {}
        self.agent = self.identity()
        self.referee = self.identity()

    def identity(self):
        key = Ed25519PrivateKey.generate()
        tagged = b"\xed\x01" + key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        number, encoded = int.from_bytes(tagged, "big"), ""
        while number:
            number, remainder = divmod(number, 58)
            encoded = alphabet[remainder] + encoded
        did = "did:key:z" + encoded
        self.keys[did] = key
        return did

    def message(self, room, did, payload, seq, nonce=None):
        if isinstance(payload, dict):
            text = json.dumps(payload, separators=(",", ":"))
        else:
            text = payload
        nonce = str(nonce or (seq + 100))
        sig = self.keys[did].sign(f"{room}|{nonce}|{text}".encode())
        import base64
        signature = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return {"seq": seq, "ts": "2026-09-25T13:00:00Z", "from": did,
                "nonce": nonce, "sig": signature, "text": text}

    def response(self, room, messages, generation=1, first_seq=None, last_seq=None):
        seqs = [m["seq"] for m in messages]
        return {"room": room, "generation": generation,
                "first_seq": first_seq if first_seq is not None else (min(seqs) if seqs else 0),
                "last_seq": last_seq if last_seq is not None else (max(seqs) if seqs else 0),
                "messages": messages}

    def referee_message(self, room, kind, seq, sweep):
        payload = {"t": kind, "n": sweep}
        return self.message(room, self.referee, payload, seq)

    def run_fake_pass(self, state_path, transcript_path, responses):
        def fetch(room, since=None, wait=0):
            item = responses[room]
            return item.pop(0)
        return observer.observe_once(state_path, transcript_path, fetch)

    def test_bootstrap_marks_bounded_tail_incomplete_and_records_junk(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            transcript = Path(directory) / "transcript.jsonl"
            responses = {}
            junk = self.message("close1", self.agent, "hello everyone", 51)
            responses["close1"] = [self.response("close1", [junk], first_seq=2, last_seq=51)]
            for room in protocol.REFEREE_ROOMS:
                kind = room.split("-")[-1]
                responses[room] = [self.response(room, [self.referee_message(room, kind, 12, 12)],
                                                 first_seq=12, last_seq=12)]
            with patch.object(protocol, "OBSERVED_REFEREE_DID", self.referee):
                result = self.run_fake_pass(state, transcript, responses)
            self.assertFalse(result["complete"])
            self.assertEqual(result["coverage"], "tail_only")
            rows = [json.loads(line) for line in transcript.read_text().splitlines()]
            self.assertTrue(any(row["validation"] == "ignored" for row in rows))

    def test_incremental_signed_owner_archived_and_cursor_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            room = "close1"
            state = observer.load_state(state_path)
            bootstrap = self.response(room, [], generation=3, first_seq=5, last_seq=5)
            observer.initialize_room(room, bootstrap, state, transcript)
            observer.atomic_json(state_path, state)
            owner_text = protocol.owner_text(self.agent)
            owner = self.message(room, self.agent, owner_text, 6)
            response = self.response(room, [owner], generation=3, first_seq=6, last_seq=6)
            result = observer.ingest_room(room, state, state_path, transcript,
                                          lambda _room, since=None: response)
            self.assertEqual(result["records"], 1)
            self.assertEqual(observer.load_state(state_path)["rooms"][room]["seq"], 6)
            self.assertEqual(json.loads(transcript.read_text().splitlines()[-1])["validation"], "participant")

    def test_transport_invalid_message_is_archived_but_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            state = observer.load_state(state_path)
            observer.initialize_room("close1", self.response("close1", [], last_seq=1), state, transcript)
            bad = self.message("close1", self.agent, "hi", 2)
            bad["sig"] = "A" * 86
            result = observer.ingest_room("close1", state, state_path, transcript,
                                          lambda *_a, **_k: self.response("close1", [bad], last_seq=2))
            self.assertEqual(result["records"], 1)
            self.assertEqual(json.loads(transcript.read_text().splitlines()[-1])["validation"], "unverified")

    def test_sequence_gap_stops_ingestion_and_marks_state_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            state = observer.load_state(state_path)
            observer.initialize_room("close1", self.response("close1", [], last_seq=5), state, transcript)
            msg = self.message("close1", self.agent, "hi", 7)
            with self.assertRaisesRegex(observer.ObserverError, "sequence gap"):
                observer.ingest_room("close1", state, state_path, transcript,
                                     lambda *_a, **_k: self.response("close1", [msg], first_seq=7, last_seq=7))
            current = observer.load_state(state_path)
            self.assertFalse(current["complete"])
            self.assertEqual(current["rooms"]["close1"]["coverage"], "sequence_gap")

    def test_generation_change_stops_ingestion(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            state = observer.load_state(state_path)
            observer.initialize_room("close1", self.response("close1", [], generation=1, last_seq=5), state, transcript)
            with self.assertRaisesRegex(observer.ObserverError, "generation changed"):
                observer.ingest_room("close1", state, state_path, transcript,
                                     lambda *_a, **_k: self.response("close1", [], generation=2, last_seq=5))

    def test_catch_up_follows_bounded_pages_without_skipping(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            state = observer.load_state(state_path)
            observer.initialize_room("close1", self.response("close1", [], last_seq=5), state, transcript)
            batches = []
            for start, stop in ((6, 206), (206, 406), (406, 501)):
                batch = [self.message("close1", self.agent, "ordinary chatter", n)
                         for n in range(start, stop)]
                batches.append(self.response("close1", batch, first_seq=start, last_seq=500))
            fetches = []
            def fetch(_room, since=None):
                fetches.append(since)
                return batches.pop(0)
            result = observer.ingest_room("close1", state, state_path, transcript, fetch)
            self.assertEqual(result["records"], 495)
            self.assertEqual(fetches, [5, 205, 405])
            self.assertEqual(observer.load_state(state_path)["rooms"]["close1"]["seq"], 500)

    def test_bad_referee_signature_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, transcript = Path(directory)/"state.json", Path(directory)/"transcript.jsonl"
            state = observer.load_state(state_path)
            room = "d-close1-state"
            msg = self.referee_message(room, "state", 10, 10)
            msg["sig"] = "A" * 86
            with patch.object(protocol, "OBSERVED_REFEREE_DID", self.referee):
                with self.assertRaises(protocol.ProtocolError):
                    observer.initialize_room(room, self.response(room, [msg], last_seq=10), state, transcript)

    def test_http_get_only_allowlist_and_redirect_refusal(self):
        with patch.object(observer.urllib.request.OpenerDirector, "open") as opened:
            opened.side_effect = AssertionError("should reject before HTTP")
            with self.assertRaises(observer.ObserverError):
                observer.get_room("tclk-offers")
        handler = observer._NoRedirect()
        with self.assertRaises(observer.ObserverError):
            handler.redirect_request(
                type("Request", (), {"get_method": lambda self: "GET"})(), None,
                302, "found", {}, "https://other.example/",
            )

    def test_http_client_uses_only_pinned_get_endpoint(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def geturl(self): return "https://technocore.chat/r/close1?format=json"
            def read(self, _size): return b'{"room":"close1","generation":1,"first_seq":0,"last_seq":0,"messages":[]}'
        def open_request(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(timeout, 20)
            self.assertTrue(request.full_url.startswith("https://technocore.chat/r/close1?"))
            return FakeResponse()
        with patch.object(observer.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = open_request
            self.assertEqual(observer.get_room("close1")["last_seq"], 0)


if __name__ == "__main__":
    unittest.main()
