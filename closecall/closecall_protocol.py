"""Offline Close Call validation. No identity loading, signing, or network I/O.

Signature validity is not registration, settlement, or proof of FLOP Labs ownership.
The referee below is the observed seed signer, pending independent attribution.
Replay protection and ledger reconciliation belong to the future observer.
"""

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SEASON = "close-1"
HISTORICAL_DID = "did:key:z6MkkPtvJEneCieb8AVphWVuEcxihMs2BK9HCETMtRQjFuAv"
OBSERVED_REFEREE_DID = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
PACKAGE_COMMIT = "66c1da36538e4b1c685417d2f66922906b13fea0"
MANIFEST_SHA256 = "bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa"
TRADING_ROOM = "close1"
REFEREE_ROOMS = frozenset({
    "d-close1-price", "d-close1-flow", "d-close1-state",
    "d-close1-positions", "d-close1-pnl",
})
ALLOWED_ROOMS = REFEREE_ROOMS | {TRADING_ROOM}
MAX_TEXT_CHARS = 4096
MAX_JSON_BYTES = MAX_TEXT_CHARS * 4
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
_NONCE = re.compile(r"[1-9][0-9]{0,18}")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{85}[AQgw]")
_AMOUNT = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,2})?")
_TRADE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TERMS_FIELDS = {"id", "maker", "px", "qty", "side", "taker", "until"}


class ProtocolError(ValueError):
    """Untrusted input failed a protocol check; never include its body in errors."""


@dataclass(frozen=True)
class VerifiedRecord:
    room: str
    seq: int
    timestamp: str
    did: str
    nonce: str
    text: str
    signature: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_value):
    raise ProtocolError("non-finite JSON number")


def _no_float(_value):
    raise ProtocolError("decimal amounts must be JSON strings")


def strict_json(text):
    """Parse bounded JSON, refusing duplicate keys, NaN and excessive nesting."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ProtocolError("invalid JSON text size")
    try:
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            raise ProtocolError("invalid JSON byte size")
        result = json.loads(text, object_pairs_hook=_object, parse_constant=_no_constant,
                            parse_float=_no_float)
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 16:
                raise ProtocolError("JSON nesting exceeds limit")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProtocolError("invalid JSON") from error


def public_key(did):
    if not isinstance(did, str) or not _DID.fullmatch(did):
        raise ProtocolError("invalid Ed25519 DID")
    number = 0
    for character in did[len("did:key:z"):]:
        number = number * 58 + _B58.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(raw) != 34 or raw[:2] != b"\xed\x01":
        raise ProtocolError("invalid DID multicodec")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def _verify(did, signature, preimage):
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        raise ProtocolError("invalid signature encoding")
    raw = base64.urlsafe_b64decode(signature + "==")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != signature:
        raise ProtocolError("noncanonical signature")
    try:
        public_key(did).verify(raw, preimage)
    except InvalidSignature as error:
        raise ProtocolError("signature verification failed") from error


def _exact_fields(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        raise ProtocolError("unexpected message fields")


def _positive_integer(value):
    return type(value) is int and value > 0


def verify_transport(room, message):
    """Verify exact stored bytes; seq/ts are server metadata, NOT signed facts."""
    if not isinstance(room, str) or room not in ALLOWED_ROOMS:
        raise ProtocolError("room is not allowed")
    if not isinstance(message, dict):
        raise ProtocolError("invalid transport record")
    seq = message.get("seq")
    if not _positive_integer(seq):
        raise ProtocolError("invalid sequence")
    timestamp = message.get("ts")
    if not isinstance(timestamp, str) or len(timestamp) > 40:
        raise ProtocolError("invalid server timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProtocolError("invalid server timestamp") from error
    if parsed.tzinfo is None:
        raise ProtocolError("server timestamp has no timezone")
    nonce = message.get("nonce")
    if type(nonce) is int:
        nonce = str(nonce)
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise ProtocolError("invalid transport nonce")
    text = message.get("text")
    if not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT_CHARS:
        raise ProtocolError("invalid transport text size")
    if text != text.strip() or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"} for c in text
    ):
        raise ProtocolError("transport text would change during server cleaning")
    did, signature = message.get("from"), message.get("sig")
    _verify(did, signature, f"{room}|{nonce}|{text}".encode("utf-8"))
    return VerifiedRecord(room, seq, timestamp, did, nonce, text, signature)


def owner_text(did=HISTORICAL_DID):
    """Prepare unsigned text only. No key or nonce is read or allocated."""
    public_key(did)
    return json.dumps({"key": did, "season": SEASON, "t": "owner"},
                      sort_keys=True, separators=(",", ":"))


def _amount(value):
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        raise ProtocolError("amount must be a positive decimal string with at most two decimals")
    result = Decimal(value)
    if result <= 0:
        raise ProtocolError("amount must be positive")
    return result


def canonical_terms(terms):
    """Validate and serialize terms; this does not check funds or eligibility."""
    _exact_fields(terms, _TERMS_FIELDS)
    if not isinstance(terms["id"], str) or not _TRADE_ID.fullmatch(terms["id"]):
        raise ProtocolError("invalid trade id")
    public_key(terms["maker"])
    if terms["taker"] != "any":
        public_key(terms["taker"])
    if terms["side"] not in ("buy", "sell"):
        raise ProtocolError("invalid maker side")
    _amount(terms["px"])
    if _amount(terms["qty"]) < Decimal("0.1"):
        raise ProtocolError("quantity is below minimum")
    if not _positive_integer(terms["until"]):
        raise ProtocolError("invalid last sweep")
    return json.dumps(terms, sort_keys=True, separators=(",", ":"))


def verify_participant_message(room, message):
    """Authenticate owner/trade messages, without treating them as accepted.

Unknown kinds (including tclk, payment, room registration and offer conventions)
are deliberately unsupported in this first, offline phase.
"""
    record = verify_transport(room, message)
    if room != TRADING_ROOM:
        raise ProtocolError("participant message is outside the trading room")
    payload = strict_json(record.text)
    if not isinstance(payload, dict):
        raise ProtocolError("message must be an object")
    # Envelope key order is not prescribed. Preserve it while checking compactness.
    if record.text != json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False):
        raise ProtocolError("message must be compact JSON")
    if payload.get("season") != SEASON:
        raise ProtocolError("wrong season")
    if payload.get("t") == "owner":
        _exact_fields(payload, {"key", "season", "t"})
        if payload["key"] != record.did:
            raise ProtocolError("owner must register its own key")
    elif payload.get("t") == "trade":
        _exact_fields(payload, {"t", "season", "terms", "taker", "maker_sig", "taker_sig"})
        terms = payload["terms"]
        canonical = canonical_terms(terms)
        taker = payload["taker"]
        public_key(taker)
        if terms["taker"] != "any" and terms["taker"] != taker:
            raise ProtocolError("wrong countersigner")
        if record.did not in (terms["maker"], taker):
            raise ProtocolError("trade publisher is not a participant")
        _verify(terms["maker"], payload["maker_sig"], f"{SEASON}|terms|{canonical}".encode())
        _verify(taker, payload["taker_sig"], f"{SEASON}|accept|{canonical}|{taker}".encode())
    else:
        raise ProtocolError("unsupported participant message")
    return record, payload


def verify_seed(message):
    """Verify the pinned observed seed; not an independent launch attestation."""
    record = verify_transport("d-close1-price", message)
    if record.did != OBSERVED_REFEREE_DID:
        raise ProtocolError("unexpected seed signer")
    payload = strict_json(record.text)
    required = {"t", "season", "price", "trade", "package", "rooms"}
    if not isinstance(payload, dict) or not required <= set(payload):
        raise ProtocolError("missing seed fields")
    if set(payload) - required - {"for", "limits"}:
        raise ProtocolError("unexpected seed fields")
    if payload["t"] != "seed" or payload["season"] != SEASON:
        raise ProtocolError("wrong seed season or type")
    if payload["package"] != MANIFEST_SHA256:
        raise ProtocolError("unexpected package hash")
    rooms = payload["rooms"]
    if (not isinstance(rooms, list) or len(rooms) != len(REFEREE_ROOMS)
            or any(not isinstance(room, str) for room in rooms)
            or set(rooms) != REFEREE_ROOMS):
        raise ProtocolError("unexpected referee rooms")
    _amount(payload["price"])
    _exact_fields(payload["trade"], {"time", "tid"})
    # The observation pin below prevents treating an arbitrary later seed as launch.
    if (record.seq != 1 or record.nonce != "1790337922535"
            or payload["price"] != "226.14"
            or payload["trade"] != {"time": "2026-09-25T11:59:42.666000Z", "tid": 626256716983248}
            or payload.get("for") != 1 or payload.get("limits") != ["214.84", "237.44"]):
        raise ProtocolError("seed differs from audited observation")
    return record, payload


def verify_referee_message(room, message):
    """Authenticate a known referee record; this does not prove referee authority."""
    if room not in REFEREE_ROOMS:
        raise ProtocolError("not a referee room")
    record = verify_transport(room, message)
    if record.did != OBSERVED_REFEREE_DID:
        raise ProtocolError("unexpected referee signer")
    payload = strict_json(record.text)
    if not isinstance(payload, dict) or not isinstance(payload.get("t"), str):
        raise ProtocolError("invalid referee message")
    expected_types = {
        "d-close1-price": {"seed", "price", "final"},
        "d-close1-flow": {"flow"},
        "d-close1-state": {"state"},
        "d-close1-positions": {"positions"},
        "d-close1-pnl": {"pnl"},
    }
    if payload["t"] not in expected_types[room]:
        raise ProtocolError("message type does not match referee room")
    if payload["t"] == "seed":
        return verify_seed(message)
    sweep = payload.get("n", payload.get("for"))
    if not _positive_integer(sweep):
        raise ProtocolError("invalid referee sweep")
    return record, payload


def verify_manifest_bytes(raw):
    """Check original manifest bytes against the seed pin; no artifact execution."""
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ProtocolError("manifest hash mismatch")
