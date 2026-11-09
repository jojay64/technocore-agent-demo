import argparse
import base64
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


BASE_URL = os.getenv("TECHNOCORE_URL", "https://technocore.chat").rstrip("/")
ROOM = os.getenv("PULSE_ROOM", "lobby")
IDENTITY_FILE = Path(os.getenv("PULSE_IDENTITY_FILE", "pulse_identity.json"))
STATE_FILE = Path(os.getenv("PULSE_STATE_FILE", ".cache/technocore_pulse_state.json"))
REPORT_DIR = Path(os.getenv("PULSE_REPORT_DIR", "pulse_reports"))
HTTP_TIMEOUT_SECONDS = 25
WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_INTERVAL_SECONDS = 5 * 60
MAX_SAMPLES = 2000
MAX_RECENT_HASHES = 5000


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value):
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def base58_encode(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or "1")


def base58_decode(value):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        try:
            digit = alphabet.index(character)
        except ValueError as error:
            raise ValueError("invalid base58 character") from error
        number = number * 58 + digit
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw


def public_key_to_did(public_key):
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # did:key Ed25519 multicodec prefix: 0xed01.
    return "did:key:z" + base58_encode(b"\xed\x01" + raw)


def did_to_public_key(did):
    prefix = "did:key:z"
    if not isinstance(did, str) or not did.startswith(prefix):
        raise ValueError("report does not contain a did:key identifier")
    decoded = base58_decode(did[len(prefix):])
    if len(decoded) != 34 or decoded[:2] != b"\xed\x01":
        raise ValueError("DID is not an Ed25519 did:key")
    return ed25519.Ed25519PublicKey.from_public_bytes(decoded[2:])


def atomic_write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize_identity(path=IDENTITY_FILE):
    if path.exists():
        raise FileExistsError(f"identity already exists: {path}")
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    did = public_key_to_did(private_key.public_key())
    atomic_write_json(
        path,
        {
            "type": "Ed25519",
            "did": did,
            "private_key_b64url": b64url_encode(raw_private),
            "created_at": utc_now(),
            "purpose": "Technocore Pulse report signing only",
        },
    )
    return did


def load_identity(path=IDENTITY_FILE):
    with path.open("r", encoding="utf-8") as handle:
        identity = json.load(handle)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        b64url_decode(identity["private_key_b64url"])
    )
    derived_did = public_key_to_did(private_key.public_key())
    if identity.get("did") != derived_did:
        raise ValueError("Pulse identity DID does not match its private key")
    return private_key, derived_did


def default_state():
    return {
        "version": 1,
        "samples": [],
        "recent_message_hashes": [],
        "last_observed_seq": 0,
    }


def load_state(path=STATE_FILE):
    if not path.exists():
        return default_state()
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("state root is not an object")
        state.setdefault("samples", [])
        state.setdefault("recent_message_hashes", [])
        state.setdefault("last_observed_seq", 0)
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        print("WARNING: invalid Pulse state; starting clean", file=sys.stderr)
        return default_state()


def prune_state(state, now=None):
    now = time.time() if now is None else now
    cutoff = now - WINDOW_SECONDS
    state["samples"] = [
        sample
        for sample in state.get("samples", [])[-MAX_SAMPLES:]
        if float(sample.get("observed_at_unix", 0)) >= cutoff
    ]
    state["recent_message_hashes"] = state.get("recent_message_hashes", [
    ])[-MAX_RECENT_HASHES:]


def request_json(path):
    url = BASE_URL + path
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Technocore-Pulse/0.1",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            status = int(response.status)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            return {
                "ok": 200 <= status < 300,
                "status": status,
                "latency_ms": latency_ms,
                "payload": payload,
                "error": None,
            }
    except urllib.error.HTTPError as error:
        return {
            "ok": False,
            "status": int(error.code),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "payload": None,
            "error": f"HTTP {error.code}",
        }
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {
            "ok": False,
            "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "payload": None,
            "error": type(error).__name__,
        }


def extract_messages(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("messages", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def message_sender(message):
    return str(message.get("from") or message.get("sender") or "")


def message_text(message):
    return str(message.get("text") or message.get("message") or "")


def message_sequence(message):
    try:
        return int(message.get("seq", 0))
    except (TypeError, ValueError):
        return 0


def normalized_hash(text):
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def analyse_messages(messages, state):
    sequences = sorted(seq for seq in map(message_sequence, messages) if seq > 0)
    senders = [message_sender(message) for message in messages]
    signed_dids = {sender for sender in senders if sender.startswith("did:key:z")}
    hashes = [normalized_hash(message_text(message)) for message in messages]
    exact_duplicates = max(0, len(hashes) - len(set(hashes)))

    previous_hashes = set(state.get("recent_message_hashes", []))
    repeated_from_previous = sum(1 for value in hashes if value in previous_hashes)

    internal_gaps = 0
    for left, right in zip(sequences, sequences[1:]):
        if right > left + 1:
            internal_gaps += right - left - 1

    previous_seq = int(state.get("last_observed_seq", 0) or 0)
    cursor_gap = 0
    if sequences and previous_seq and sequences[0] > previous_seq + 1:
        cursor_gap = sequences[0] - previous_seq - 1

    if sequences:
        state["last_observed_seq"] = max(previous_seq, sequences[-1])
    state["recent_message_hashes"].extend(hashes)

    total = len(messages)
    signed_messages = sum(1 for sender in senders if sender.startswith("did:key:z"))
    return {
        "messages_observed": total,
        "first_seq": sequences[0] if sequences else None,
        "last_seq": sequences[-1] if sequences else None,
        "sequence_gaps_observed": internal_gaps + cursor_gap,
        "unique_senders": len(set(senders)),
        "unique_signed_dids": len(signed_dids),
        "signed_message_share": round(signed_messages / total, 4) if total else None,
        "exact_duplicate_share": round(exact_duplicates / total, 4) if total else None,
        "repeated_from_previous_sample": repeated_from_previous,
    }


def collect_sample(state):
    observed_at = time.time()
    room_name = urllib.parse.quote(ROOM, safe="")
    paths = {
        "health": "/healthz",
        "rooms": "/rooms?format=json&limit=50",
        "room": f"/r/{room_name}?format=json&limit=200",
    }
    # Independent public reads run together, keeping one failed probe bounded by
    # one HTTP timeout rather than three sequential timeouts.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            name: executor.submit(request_json, path)
            for name, path in paths.items()
        }
        results = {name: future.result() for name, future in futures.items()}
    health = results["health"]
    rooms = results["rooms"]
    room = results["room"]
    messages = extract_messages(room.get("payload"))

    sample = {
        "observed_at": datetime.fromtimestamp(observed_at, timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "observed_at_unix": observed_at,
        "base_url": BASE_URL,
        "room": ROOM,
        "endpoints": {
            "health": {
                key: health[key]
                for key in ("ok", "status", "latency_ms", "error")
            },
            "rooms": {
                key: rooms[key]
                for key in ("ok", "status", "latency_ms", "error")
            },
            "room": {
                key: room[key]
                for key in ("ok", "status", "latency_ms", "error")
            },
        },
        "room_metrics": analyse_messages(messages, state) if room["ok"] else None,
    }
    sample["available"] = all(
        sample["endpoints"][name]["ok"] for name in ("health", "rooms", "room")
    )
    state["samples"].append(sample)
    prune_state(state, observed_at)
    return sample


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(float(ordered[index]), 2)


def build_report(state, did):
    prune_state(state)
    samples = state.get("samples", [])
    available = sum(1 for sample in samples if sample.get("available"))
    latencies = []
    status_counts = {}
    total_messages = 0
    total_gaps = 0
    unique_dids_peak = 0
    duplicate_shares = []

    for sample in samples:
        for endpoint in sample.get("endpoints", {}).values():
            status = str(endpoint.get("status") or endpoint.get("error") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            if endpoint.get("ok") and endpoint.get("latency_ms") is not None:
                latencies.append(float(endpoint["latency_ms"]))
        metrics = sample.get("room_metrics") or {}
        total_messages += int(metrics.get("messages_observed") or 0)
        total_gaps += int(metrics.get("sequence_gaps_observed") or 0)
        unique_dids_peak = max(unique_dids_peak, int(metrics.get("unique_signed_dids") or 0))
        if metrics.get("exact_duplicate_share") is not None:
            duplicate_shares.append(float(metrics["exact_duplicate_share"]))

    report = {
        "schema": "technocore-pulse-report-v1",
        "generated_at": utc_now(),
        "observer_did": did,
        "scope": {
            "base_url": BASE_URL,
            "room": ROOM,
            "window_seconds": WINDOW_SECONDS,
        },
        "sample_count": len(samples),
        "availability_share": round(available / len(samples), 4) if samples else None,
        "endpoint_status_counts": dict(sorted(status_counts.items())),
        "latency_ms": {
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p90": percentile(latencies, 0.90),
            "maximum": round(max(latencies), 2) if latencies else None,
        },
        "room_observations": {
            "messages_scanned_across_samples": total_messages,
            "sequence_gaps_observed": total_gaps,
            "peak_unique_signed_dids_in_one_sample": unique_dids_peak,
            "mean_exact_duplicate_share": (
                round(statistics.fmean(duplicate_shares), 4)
                if duplicate_shares
                else None
            ),
        },
        "limitations": [
            "Measurements reflect this observer's network path and sampling schedule.",
            "Room reads are rolling windows, not complete history.",
            "Server sequence and timestamp fields are observations, not DID-signed claims.",
            "No message body is retained in Pulse state or reports.",
        ],
    }
    return report


def sign_report(report, private_key):
    unsigned = dict(report)
    unsigned.pop("signature", None)
    signature = private_key.sign(canonical_json(unsigned))
    signed = dict(unsigned)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "encoding": "base64url-no-pad",
        "value": b64url_encode(signature),
    }
    return signed


def verify_report(report):
    signature_block = report.get("signature")
    if not isinstance(signature_block, dict):
        raise ValueError("report has no signature block")
    if signature_block.get("algorithm") != "Ed25519":
        raise ValueError("unsupported report signature algorithm")
    unsigned = dict(report)
    unsigned.pop("signature", None)
    public_key = did_to_public_key(unsigned.get("observer_did"))
    try:
        public_key.verify(
            b64url_decode(signature_block.get("value", "")),
            canonical_json(unsigned),
        )
    except (InvalidSignature, ValueError) as error:
        raise ValueError("invalid Pulse report signature") from error
    return True


def save_report(report, directory=REPORT_DIR, archive=False):
    directory.mkdir(parents=True, exist_ok=True)
    latest_path = directory / "pulse-latest.json"
    atomic_write_json(latest_path, report, mode=0o644)
    if not archive:
        return latest_path
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"pulse-report-{timestamp}.json"
    atomic_write_json(path, report, mode=0o644)
    return path


def print_sample(sample):
    availability = "UP" if sample["available"] else "DEGRADED"
    statuses = ", ".join(
        f"{name}={details.get('status') or details.get('error')}"
        for name, details in sample["endpoints"].items()
    )
    print(f"PULSE {sample['observed_at']} {availability} {statuses}")


def run_probe():
    private_key, did = load_identity()
    state = load_state()
    sample = collect_sample(state)
    atomic_write_json(STATE_FILE, state)
    report = sign_report(build_report(state, did), private_key)
    verify_report(report)
    report_path = save_report(report, archive=True)
    print_sample(sample)
    print("Signed report:", report_path)
    print("Observer DID:", did)


def run_forever(interval_seconds):
    private_key, did = load_identity()
    print("Technocore Pulse started")
    print("Observer DID:", did)
    print("Room:", ROOM)
    print("Interval:", interval_seconds, "seconds")
    print("Security: read-only; message bodies are untrusted and never retained")
    while True:
        started = time.monotonic()
        try:
            state = load_state()
            sample = collect_sample(state)
            report = sign_report(build_report(state, did), private_key)
            verify_report(report)
            report_day = report["generated_at"][:10]
            archive = state.get("last_archive_date") != report_day
            if archive:
                state["last_archive_date"] = report_day
            atomic_write_json(STATE_FILE, state)
            report_path = save_report(report, archive=archive)
            print_sample(sample)
            print("REPORT", report_path, flush=True)
        except Exception as error:
            print(f"PULSE ERROR: {type(error).__name__}: {str(error)[:200]}", flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, interval_seconds - elapsed))


def command_verify(path):
    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    verify_report(report)
    print("VALID Pulse report")
    print("Observer DID:", report["observer_did"])
    print("Generated at:", report["generated_at"])


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only Technocore availability and room-signal observer"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create the dedicated Pulse DID once")
    subparsers.add_parser("probe", help="collect one sample and write a signed report")
    run_parser = subparsers.add_parser("run", help="collect continuously")
    run_parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between samples (minimum 60)",
    )
    verify_parser = subparsers.add_parser("verify", help="verify a signed report")
    verify_parser.add_argument("report", type=Path)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "init":
        did = initialize_identity()
        print("Pulse identity created")
        print("Public DID:", did)
        print("Private identity file:", IDENTITY_FILE)
        print("Keep that file secret and back it up; never commit it.")
    elif args.command == "probe":
        run_probe()
    elif args.command == "run":
        if args.interval < 60:
            raise SystemExit("--interval must be at least 60 seconds")
        run_forever(args.interval)
    elif args.command == "verify":
        command_verify(args.report)


if __name__ == "__main__":
    main()
