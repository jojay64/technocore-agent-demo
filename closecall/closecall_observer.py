"""Read-only Close Call observer. This module has no key or write capability."""

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import closecall_protocol as protocol


BASE_URL = "https://technocore.chat"
STATE_FILE = Path(os.getenv("CLOSECALL_STATE_FILE", ".cache/closecall/state.json"))
TRANSCRIPT_FILE = Path(os.getenv("CLOSECALL_TRANSCRIPT_FILE", ".cache/closecall/transcript.jsonl"))
MAX_RESPONSE_BYTES = 1024 * 1024
USER_AGENT = "technocore-closecall-observer/0.1"


class ObserverError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path=STATE_FILE):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "season": protocol.SEASON, "coverage": "uninitialized",
                "complete": False, "rooms": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("season") != protocol.SEASON or not isinstance(value.get("rooms"), dict)):
        raise ObserverError("local observer state invalid; refusing to reset cursors")
    return value


def get_room(room, since=None, wait=0):
    if room not in protocol.ALLOWED_ROOMS:
        raise ObserverError("room is outside the pinned allowlist")
    params = {"format": "json", "limit": "200"}
    if since is not None:
        params["since"] = str(since)
    if wait:
        params["wait"] = str(wait)
    url = f"{BASE_URL}/r/{urllib.parse.quote(room, safe='')}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if response.geturl().split("/", 3)[:3] != ["https:", "", "technocore.chat"]:
                raise ObserverError("read redirected outside the pinned service")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ObserverError("room read failed") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ObserverError("room response exceeds size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ObserverError("invalid room response") from error
    if (not isinstance(payload, dict) or payload.get("room") != room
            or type(payload.get("generation")) is not int or payload["generation"] < 1
            or type(payload.get("first_seq")) is not int or payload["first_seq"] < 0
            or type(payload.get("last_seq")) is not int or payload["last_seq"] < 0
            or not isinstance(payload.get("messages"), list)):
        raise ObserverError("room response does not match requested shape")
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ObserverError("redirect refused")


def verify_message(room, message):
    if room == protocol.TRADING_ROOM:
        try:
            return "participant", *protocol.verify_participant_message(room, message)
        except protocol.ProtocolError as error:
            try:
                record = protocol.verify_transport(room, message)
            except protocol.ProtocolError:
                return "unverified", None, None, str(error)
            return "ignored", record, None, str(error)
    record, payload = protocol.verify_referee_message(room, message)
    return "referee", record, payload


def append_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def initialize_room(room, response, state, transcript_path):
    generation = response["generation"]
    messages = response["messages"]
    first_seq, last_seq = response["first_seq"], response["last_seq"]
    if any(not isinstance(m, dict) or type(m.get("seq")) is not int for m in messages):
        raise ObserverError("invalid bootstrap messages")
    ordered = sorted(messages, key=lambda m: m["seq"])
    if any(a["seq"] >= b["seq"] for a, b in zip(ordered, ordered[1:])):
        raise ObserverError("duplicate or unordered bootstrap sequence")
    for message in ordered:
        result = verify_message(room, message)
        append_record(transcript_path, {
            "kind": "bootstrap_tail", "room": room, "generation": generation,
            "validation": result[0], "record": message,
            **({"reason": result[3][:160]} if result[0] in {"ignored", "unverified"} else {}),
        })
    # The API supplies only a bounded tail. Bootstrap records are observations, not
    # sufficient history for a position/credit/score claim.
    state["rooms"][room] = {"generation": generation, "seq": last_seq,
                            "coverage": "tail_only", "first_retained": first_seq}
    state["complete"] = False
    state["coverage"] = "tail_only"


def ingest_room(room, state, state_path=STATE_FILE, transcript_path=TRANSCRIPT_FILE,
                fetch=get_room):
    cursor = state["rooms"].get(room)
    response = fetch(room, since=cursor["seq"] if cursor else 0)
    if cursor is None:
        initialize_room(room, response, state, transcript_path)
        atomic_json(state_path, state)
        return {"room": room, "status": "tail_only", "records": len(response["messages"]),
                "seq": response["last_seq"]}
    if response["generation"] != cursor["generation"]:
        cursor["coverage"] = "generation_changed"
        state["complete"] = False
        atomic_json(state_path, state)
        raise ObserverError("room generation changed; manual reconciliation required")
    count = 0
    page = response
    # Bounded catch-up: follow full 200-record pages, but yield after five reads so
    # one busy public room cannot starve the referee rooms or burn the read quota.
    for _ in range(5):
        messages = page["messages"]
        expected = cursor["seq"] + 1
        for message in messages:
            if not isinstance(message, dict) or type(message.get("seq")) is not int:
                raise ObserverError("invalid incremental record")
            seq = message["seq"]
            if seq <= cursor["seq"]:
                continue
            if seq != expected:
                cursor["coverage"] = "sequence_gap"
                state["complete"] = False
                state["coverage"] = "incomplete"
                atomic_json(state_path, state)
                raise ObserverError("room history has a sequence gap; manual reconciliation required")
            result = verify_message(room, message)
            append_record(transcript_path, {
                "kind": "verified", "room": room, "generation": cursor["generation"],
                "validation": result[0], "record": message,
                **({"reason": result[3][:160]} if result[0] in {"ignored", "unverified"} else {}),
            })
            cursor["seq"] = seq
            expected = seq + 1
            count += 1
            atomic_json(state_path, state)
        if cursor["seq"] >= page["last_seq"] or len(messages) < 200:
            break
        page = fetch(room, since=cursor["seq"])
        if page["generation"] != cursor["generation"]:
            cursor["coverage"] = "generation_changed"
            state["complete"] = False
            atomic_json(state_path, state)
            raise ObserverError("room generation changed; manual reconciliation required")
    if cursor["seq"] < page["last_seq"] and not page["messages"]:
        cursor["coverage"] = "sequence_gap"
        state["complete"] = False
        state["coverage"] = "incomplete"
        atomic_json(state_path, state)
        raise ObserverError("room response omitted records; manual reconciliation required")
    return {"room": room, "status": cursor["coverage"], "records": count,
            "seq": cursor["seq"]}


def observe_once(state_path=STATE_FILE, transcript_path=TRANSCRIPT_FILE, fetch=get_room):
    state = load_state(state_path)
    results = []
    for room in (protocol.TRADING_ROOM, *sorted(protocol.REFEREE_ROOMS)):
        results.append(ingest_room(room, state, state_path, transcript_path, fetch))
    return {"mode": "OBSERVE", "coverage": state["coverage"],
            "complete": state["complete"], "rooms": results}


def main():
    parser = argparse.ArgumentParser(description="Read-only Close Call transcript observer")
    parser.add_argument("--once", action="store_true", help="perform one read-only pass")
    parser.add_argument("--interval", type=int, default=30, help="poll interval, 5..300 seconds")
    args = parser.parse_args()
    if not 5 <= args.interval <= 300:
        parser.error("interval must be between 5 and 300 seconds")
    while True:
        try:
            result = observe_once()
            print(json.dumps(result, separators=(",", ":")), flush=True)
        except (ObserverError, protocol.ProtocolError, OSError, ValueError) as error:
            print(json.dumps({"mode": "OBSERVE", "status": "blocked", "reason": str(error)},
                             separators=(",", ":")), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
