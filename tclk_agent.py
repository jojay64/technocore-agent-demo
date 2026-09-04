"""Guarded tclk/1 PAPER transcript agent.

This phase is deliberately observe-only.  It evaluates new PAPER text offers,
keeps complete signed transport records for approved candidates, follows their
contracts, and folds heartbeat-aware state.  It contains no write endpoint,
accept builder, secret, settlement, or identity-generation path.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
from pathlib import Path

import tclk_offer_watcher as guard


OFFER_ROOM = "tclk-offers"
IDENTITY_FILE = Path(os.getenv("TCLK_IDENTITY_FILE", "research_identity.json"))
STATE_FILE = Path(os.getenv("TCLK_AGENT_STATE_FILE", ".cache/tclk_agent_state.json"))
DECISION_LOG = Path(os.getenv("TCLK_DECISION_LOG", ".cache/tclk_agent_decisions.jsonl"))
TRANSCRIPT_LOG = Path(os.getenv("TCLK_TRANSCRIPT_LOG", ".cache/tclk_signed_transcripts.jsonl"))
EXPECTED_DID = guard.MY_DID
MAX_CONTRACTS = 100
MAX_OFFERS = 300
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
FRAME_NONCE = re.compile(r"^[0-9a-f]{8,64}$")

FRAME_FIELDS = {
    "accept": ({"type", "from", "ref", "statement", "contract", "paymentKey", "nonce"},
               {"type", "from", "ref", "statement", "contract", "nonce"}),
    "lock": ({"type", "from", "contract", "rail", "ref", "presig"},
             {"type", "from", "contract", "rail", "ref"}),
    "reveal": ({"type", "from", "contract", "secret", "ref"},
               {"type", "from", "contract", "secret"}),
    "refund": ({"type", "from", "contract", "ref", "reason"},
               {"type", "from", "contract"}),
    "cancel": ({"type", "from", "contract", "reason"},
               {"type", "from", "contract"}),
    "receipt": ({"type", "from", "contract", "outcome", "rail", "ref"},
                {"type", "from", "contract", "outcome"}),
    "heartbeat": ({"type", "from", "contract", "nonce", "note"},
                  {"type", "from", "contract", "nonce"}),
}


def append_jsonl(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def clean_state():
    return {
        "version": 1,
        "initialized": False,
        "room_sequences": {OFFER_ROOM: 0},
        "candidate_offers": {},
        "contracts": {},
    }


def load_state():
    if not STATE_FILE.exists():
        return clean_state()
    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("WARNING: invalid tclk agent state; failing closed with clean state")
        return clean_state()
    state = clean_state()
    if isinstance(loaded, dict):
        state.update(loaded)
    if not isinstance(state.get("room_sequences"), dict):
        state["room_sequences"] = {OFFER_ROOM: 0}
    if not isinstance(state.get("candidate_offers"), dict):
        state["candidate_offers"] = {}
    if not isinstance(state.get("contracts"), dict):
        state["contracts"] = {}
    state["room_sequences"].setdefault(OFFER_ROOM, 0)
    return state


def save_state(state):
    state["candidate_offers"] = dict(list(state["candidate_offers"].items())[-MAX_OFFERS:])
    state["contracts"] = dict(list(state["contracts"].items())[-MAX_CONTRACTS:])
    guard.atomic_json(STATE_FILE, state)


def verify_historical_identity():
    if not IDENTITY_FILE.exists():
        raise RuntimeError(f"Missing {IDENTITY_FILE}; identity creation is forbidden")
    try:
        identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid {IDENTITY_FILE}") from error
    if identity.get("did") != EXPECTED_DID:
        raise RuntimeError("Historical Research DID mismatch; refusing to start")
    private_hex = identity.get("private_key_hex")
    if not isinstance(private_hex, str) or len(private_hex) != 64:
        raise RuntimeError("Historical Research private key is missing or malformed")
    try:
        private_bytes = bytes.fromhex(private_hex)
        derived = guard.ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
        public = derived.public_key().public_bytes_raw()
    except (ValueError, TypeError) as error:
        raise RuntimeError("Historical Research private key is invalid") from error
    tagged = guard.base58decode(EXPECTED_DID[len("did:key:z"):])
    if tagged != b"\xed\x01" + public:
        raise RuntimeError("Historical Research key does not match its DID")
    return EXPECTED_DID


def room_messages(room, since, wait_seconds=None):
    if not ROOM_RE.fullmatch(room):
        raise ValueError("invalid room name")
    if wait_seconds is None:
        wait_seconds = guard.LONG_POLL_SECONDS if room == OFFER_ROOM else 0
    query = urllib.parse.urlencode(
        {"since": int(since), "wait": int(wait_seconds), "format": "json"}
    )
    data = json.loads(guard.read_url(f"{guard.BASE_URL}/r/{room}?{query}", 1024 * 1024))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "items", "records"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def transport_record(room, message):
    if not isinstance(message, dict):
        raise ValueError("record is not an object")
    normalized = guard.extract_record(message)
    normalized.update({
        "room": room,
        "ts": message.get("ts"),
        "line": normalized.pop("text"),
    })
    return normalized


def verify_transport(record):
    payload = f"{record['room']}|{record['nonce']}|{record['line']}".encode("utf-8")
    try:
        guard.did_public_key(record["sender"]).verify(
            guard.b64decode(record["signature"]), payload
        )
    except Exception as error:
        raise ValueError("transport signature does not verify") from error


def decode_frame(line):
    if not isinstance(line, str) or not line.startswith("tclk1 "):
        return None
    if len(line) > guard.MAX_FRAME_CHARS or "\n" in line or "\r" in line:
        raise ValueError("frame exceeds the single-line limit")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in line):
        raise ValueError("frame is not printable ASCII")
    try:
        frame = json.loads(line[6:])
    except json.JSONDecodeError as error:
        raise ValueError("frame is not valid JSON") from error
    if not isinstance(frame, dict) or line != "tclk1 " + guard.canonical_json(frame):
        raise ValueError("frame JSON is not canonical")
    if frame.get("type") == "offer":
        return guard.parse_offer(line)
    frame_type = frame.get("type")
    if frame_type not in FRAME_FIELDS:
        raise ValueError("unknown frame type")
    allowed, required = FRAME_FIELDS[frame_type]
    if set(frame) - allowed or required - set(frame):
        raise ValueError(f"{frame_type} has unknown or missing fields")
    if not guard.DID.fullmatch(str(frame.get("from", ""))):
        raise ValueError(f"{frame_type} sender is invalid")
    if not HEX32.fullmatch(str(frame.get("contract", ""))):
        raise ValueError(f"{frame_type} contract is invalid")
    if frame_type in {"accept", "heartbeat"} and not FRAME_NONCE.fullmatch(
        str(frame.get("nonce", ""))
    ):
        raise ValueError(f"{frame_type} nonce is invalid")
    if frame_type == "accept":
        if not HEX32.fullmatch(str(frame.get("ref", ""))):
            raise ValueError("accept ref is invalid")
        if not HEX32.fullmatch(str(frame.get("statement", ""))):
            raise ValueError("only hash-lock accepts are supported")
    if frame_type == "lock":
        if frame.get("rail") != "paper" or not isinstance(frame.get("ref"), str):
            raise ValueError("only paper locks are supported")
    if frame_type == "reveal" and not HEX32.fullmatch(str(frame.get("secret", ""))):
        raise ValueError("reveal secret is invalid")
    if frame_type == "receipt" and frame.get("outcome") not in {
        "claimed", "refunded", "cancelled"
    }:
        raise ValueError("receipt outcome is invalid")
    if frame_type == "heartbeat" and "note" in frame and not isinstance(frame["note"], str):
        raise ValueError("heartbeat note is invalid")
    return frame


def contract_id(offer, accept):
    core = {
        "from": accept["from"],
        "ref": accept["ref"],
        "statement": accept["statement"],
        "nonce": accept["nonce"],
    }
    if "paymentKey" in accept:
        core["paymentKey"] = accept["paymentKey"]
    body = "FLOP::tclk::v1|contract|" + guard.canonical_json(
        {"offer": offer, "accept": core}
    )
    return "0x" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def deal_room(contract):
    if not HEX32.fullmatch(str(contract)):
        raise ValueError("invalid contract id")
    return "mb-p-tclk-" + contract[2:18]


def transcript_entry(record, frame, state_before, state_after, valid, reason=""):
    return {
        "schema": "technocore-tclk-signed-transcript-v1",
        "recorded_at": time.time(),
        "room": record["room"],
        "seq": record["seq"],
        "timestamp": record["ts"],
        "timestamp_ms": record["timestamp_ms"],
        "sender_did": record["sender"],
        "transport_nonce": record["nonce"],
        "transport_signature": record["signature"],
        "content": record["line"],
        "frame_type": frame.get("type") if isinstance(frame, dict) else None,
        "contract": frame.get("contract") if isinstance(frame, dict) else None,
        "state_before": state_before,
        "state_after": state_after,
        "valid": bool(valid),
        "reason": reason,
    }


def apply_contract_frame(contract, frame, timestamp_ms):
    status = contract["status"]
    offer = contract["offer"]
    if frame["contract"] != contract["contract"]:
        return status, False, "frame names a different contract"
    if frame["from"] not in {contract["payer_did"], contract["payee_did"]}:
        return status, False, "frame sender is not a party"
    kind = frame["type"]
    if kind == "heartbeat":
        if status not in {"accepted", "locked"}:
            return status, False, f"heartbeat in state {status}"
        return status, True, "signed liveness only; state unchanged"
    if kind == "lock":
        if status != "accepted" or frame["from"] != contract["payer_did"]:
            return status, False, "invalid lock transition"
        if timestamp_ms >= offer["refundAfterMs"]:
            return status, False, "refund window already open"
        contract["rail_ref"] = frame["ref"]
        return "locked", True, "paper lock observed"
    if kind == "reveal":
        if status != "locked" or frame["from"] != contract["payee_did"]:
            return status, False, "invalid reveal transition"
        if "ref" in frame and frame["ref"] != contract.get("rail_ref"):
            return status, False, "reveal rail reference mismatch"
        digest = "0x" + hashlib.sha256(bytes.fromhex(frame["secret"][2:])).hexdigest()
        if digest != contract["statement"]:
            return status, False, "secret does not open statement"
        if timestamp_ms >= offer["refundAfterMs"]:
            return status, False, "refund window already open"
        return "claimed", True, "valid reveal"
    if kind == "refund":
        if status != "locked" or frame["from"] != contract["payer_did"]:
            return status, False, "invalid refund transition"
        if timestamp_ms < offer["refundAfterMs"]:
            return status, False, "refund window not open"
        return "refunded", True, "valid refund"
    if kind == "cancel":
        if status not in {"accepted"}:
            return status, False, f"cancel in state {status}"
        return "cancelled", True, "valid cancellation"
    if kind == "receipt":
        expected = {"claimed": "claimed", "refunded": "refunded", "cancelled": "cancelled"}
        if status not in expected or frame["outcome"] != expected[status]:
            return status, False, "receipt contradicts terminal state"
        return status, True, "terminal receipt; state unchanged"
    return status, False, f"unsupported {kind} transition"


def evaluate_offer(record, frame, state):
    if frame["id"] in state["candidate_offers"]:
        return
    try:
        task, source = guard.resolve_context(frame.get("job", {}).get("context", ""))
        eligible, reason = guard.deterministic_screen(record, frame, task)
    except Exception as error:
        eligible, reason, task, source = False, str(error), "", ""
    decision = {
        "logged_at": time.time(), "seq": record["seq"], "offer_id": frame["id"],
        "sender": record["sender"], "result": "filtered", "reason": reason,
    }
    if not eligible:
        append_jsonl(DECISION_LOG, decision)
        return
    research = guard.research_review(frame, task)
    critic = guard.critic_review(frame, task, research) if guard.approved(research) else {}
    judge = guard.judge_review(frame, task, research, critic) if guard.approved(critic) else {}
    approved = guard.approved(research) and guard.approved(critic) and guard.approved(judge)
    decision.update({
        "result": "candidate_approved" if approved else "rejected",
        "task_specification": task, "context_source": source,
        "research": research, "critic": critic, "judge": judge,
        "mode": "PAPER_OBSERVE_ONLY",
    })
    append_jsonl(DECISION_LOG, decision)
    if not approved:
        return
    state["candidate_offers"][frame["id"]] = {
        "offer": frame, "task": task, "source": source,
        "record": transcript_entry(record, frame, None, "proposed", True),
    }
    append_jsonl(TRANSCRIPT_LOG, state["candidate_offers"][frame["id"]]["record"])
    save_state(state)
    print(f"CANDIDATE APPROVED {frame['id']} — transcript tracking enabled; no action sent")


def process_offer_room(message, state):
    record = transport_record(OFFER_ROOM, message)
    verify_transport(record)
    frame = decode_frame(record["line"])
    if frame is None or frame["from"] != record["sender"]:
        return
    if frame["type"] == "offer":
        evaluate_offer(record, frame, state)
        return
    if frame["type"] != "accept" or frame["ref"] not in state["candidate_offers"]:
        return
    candidate = state["candidate_offers"][frame["ref"]]
    offer = candidate["offer"]
    expected = contract_id(offer, frame)
    if frame["contract"] != expected or frame["from"] == offer["from"]:
        append_jsonl(TRANSCRIPT_LOG, transcript_entry(
            record, frame, "proposed", "proposed", False, "invalid acceptance"
        ))
        return
    payer = offer["from"] if offer["role"] == "payer" else frame["from"]
    payee = frame["from"] if offer["role"] == "payer" else offer["from"]
    state["contracts"][expected] = {
        "contract": expected, "offer": offer, "accept": frame,
        "payer_did": payer, "payee_did": payee,
        "statement": frame["statement"], "status": "accepted",
        "room": deal_room(expected), "task": candidate["task"],
    }
    state["room_sequences"].setdefault(deal_room(expected), 0)
    append_jsonl(TRANSCRIPT_LOG, transcript_entry(
        record, frame, "proposed", "accepted", True
    ))
    save_state(state)


def process_deal_room(room, message, state):
    record = transport_record(room, message)
    verify_transport(record)
    frame = decode_frame(record["line"])
    if frame is None or frame.get("type") in {"offer", "accept"}:
        return
    contract = state["contracts"].get(frame.get("contract"))
    if not contract or contract["room"] != room or frame["from"] != record["sender"]:
        return
    before = contract["status"]
    after, valid, reason = apply_contract_frame(contract, frame, record["timestamp_ms"])
    if valid:
        contract["status"] = after
        contract["last_frame_type"] = frame["type"]
        contract["last_seq"] = record["seq"]
    append_jsonl(TRANSCRIPT_LOG, transcript_entry(
        record, frame, before, after if valid else before, valid, reason
    ))
    save_state(state)


def initialize_head(state):
    messages = room_messages(OFFER_ROOM, 0, wait_seconds=0)
    state["room_sequences"][OFFER_ROOM] = max(
        (item.get("seq", 0) for item in messages if isinstance(item, dict)), default=0
    )
    state["initialized"] = True
    save_state(state)


def poll_room(room, state):
    since = int(state["room_sequences"].get(room, 0))
    for message in room_messages(room, since):
        if not isinstance(message, dict):
            continue
        seq = message.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= since:
            continue
        state["room_sequences"][room] = seq
        save_state(state)
        try:
            if room == OFFER_ROOM:
                process_offer_room(message, state)
            else:
                process_deal_room(room, message, state)
        except Exception as error:
            append_jsonl(DECISION_LOG, {
                "logged_at": time.time(), "result": "record_rejected",
                "room": room, "seq": seq, "reason": str(error)[:300],
            })


def main():
    did = verify_historical_identity()
    state = load_state()
    print("=" * 72)
    print("Technocore tclk/1 guarded transcript agent")
    print("Research DID:", did)
    print("Offer room  :", OFFER_ROOM)
    print("Mode        : PAPER OBSERVE-ONLY")
    print("Pipeline    : Research -> Critic -> Judge")
    print("Heartbeat   : verify + fold + signed transcript")
    print("Actions     : DISABLED (no write path)")
    print("Identity    : existing key required; creation/rotation forbidden")
    print("=" * 72)
    if not state["initialized"]:
        print("First start: setting current offer-room head without processing history")
        initialize_head(state)
    while True:
        try:
            poll_room(OFFER_ROOM, state)
            for room in list(state["room_sequences"]):
                if room != OFFER_ROOM:
                    poll_room(room, state)
        except KeyboardInterrupt:
            print("Stopped by user")
            return
        except urllib.error.HTTPError as error:
            print("READ ERROR: HTTP", error.code)
            time.sleep(5)
        except urllib.error.URLError as error:
            print("READ ERROR:", error)
            time.sleep(5)
        except Exception as error:
            print("FAIL-CLOSED LOOP ERROR:", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
