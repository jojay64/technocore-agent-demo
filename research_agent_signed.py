import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from openai import OpenAI


ROOM = "flop-agent-lab"
KEY_FILE = "research_identity.json"

MY_DID = "did:key:z6MkkPtvJEneCieb8AVphWVuEcxihMs2BK9HCETMtRQjFuAv"
CRITIC_DID = "did:key:z6MkiLc24vTBQwtXCWw5gAfwHL7Nvy3xszK7rUpgPnaWictM"

MAX_REPLIES = 3
MAX_MESSAGE_CHARS = 600

TASK = (
    "Design one practical coordination pattern for autonomous AI agents "
    "using only these established Technocore primitives: shared rooms, "
    "incremental message reads, long-polling, persistent notes/KV state, "
    "and signed did:key identities."
)

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

client = OpenAI()


def b58(data):
    n = int.from_bytes(data, "big")
    result = []

    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(B58[remainder])

    zeros = len(data) - len(data.lstrip(b"\x00"))

    return "1" * zeros + "".join(reversed(result))


def normalize_text(text):
    return " ".join(text.split())


def prepare_message(text):
    text = normalize_text(text)

    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS]

        if " " in text:
            text = text.rsplit(" ", 1)[0]

        text += "..."

    return text


def load_or_create_identity():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            identity = json.load(f)

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(identity["private_key_hex"])
        )

        did = identity["did"]

        if did != MY_DID:
            raise RuntimeError(
                "research_identity.json does not match MY_DID."
            )

        return private_key, did

    private_key = ed25519.Ed25519PrivateKey.generate()

    raw_private = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )

    raw_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    did = "did:key:z" + b58(b"\xed\x01" + raw_public)

    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "did": did,
                "private_key_hex": raw_private.hex(),
            },
            f,
            indent=2,
        )

    return private_key, did


def send_signed_message(private_key, did, text):
    text = prepare_message(text)

    nonce = str(time.time_ns())

    payload = f"{ROOM}|{nonce}|{text}".encode("utf-8")

    signature = (
        base64.urlsafe_b64encode(
            private_key.sign(payload)
        )
        .decode()
        .rstrip("=")
    )

    encoded_text = urllib.parse.quote(text, safe="")

    url = (
        f"https://technocore.chat/r/{ROOM}/say-signed/"
        f"{did}/{signature}/{nonce}/{encoded_text}"
    )

    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status

    except urllib.error.HTTPError as e:
        print("Technocore HTTP error:", e.code)
        print("Server response:", e.read().decode())
        return e.code

    except urllib.error.URLError as e:
        print("Technocore connection error:", e)
        return None


def get_messages(since):
    url = (
        f"https://technocore.chat/r/{ROOM}"
        f"?since={since}&wait=10&format=json"
    )

    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode())

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "messages" in data:
            return data["messages"]

        if "items" in data:
            return data["items"]

    return []


def get_latest_sequence():
    messages = get_messages(0)
    sequences = []

    for message in messages:
        if not isinstance(message, dict):
            continue

        try:
            sequences.append(int(message.get("seq", 0)))
        except (TypeError, ValueError):
            continue

    return max(sequences) if sequences else 0


def generate_initial_proposal():
    response = client.responses.create(
        model="gpt-5.4-nano",
        instructions=(
            "You are research-agent. "
            "Create a concrete proposal using ONLY: shared rooms, incremental reads, "
            "long-polling, persistent notes/KV, and signed did:key identities. "
            "Distinguish application-level conventions from Technocore guarantees. "
            "Do not claim queues, locks, consensus, leader election, transactions, "
            "exactly-once delivery, confidentiality, or scheduling. "
            "If a guarantee is unknown, say it is not established. "
            "Maximum 2 short points and about 350 characters."
        ),
        input=TASK,
        max_output_tokens=90,
    )

    return prepare_message(response.output_text)


def improve_proposal(critique, final=False):
    if final:
        instruction = (
            "Produce the FINAL proposal after incorporating the critique. "
            "Maximum 2 concise points and about 350 characters."
        )
    else:
        instruction = (
            "Improve the proposal using useful criticism. "
            "Maximum 2 concise points and about 350 characters."
        )

    response = client.responses.create(
        model="gpt-5.4-nano",
        instructions=(
            "You are research-agent. "
            "The input comes from the verified critic-agent DID. "
            "Use ONLY: shared rooms, incremental reads, long-polling, "
            "persistent notes/KV, and signed did:key identities. "
            "Distinguish application conventions from platform guarantees. "
            "Never claim queues, locks, consensus, leader election, transactions, "
            "exactly-once delivery, confidentiality, or scheduling. "
            "If a guarantee is unknown, say it is not established. "
            + instruction
        ),
        input=critique,
        max_output_tokens=90,
    )

    return prepare_message(response.output_text)


private_key, did = load_or_create_identity()

print("Research agent started")
print("DID:", did)
print("Listening only to critic DID:")
print(CRITIC_DID)

since = get_latest_sequence()

print("Ignoring old history.")
print("Starting from sequence:", since)

replies_sent = 0

print()
print("Generating initial proposal...")

initial_proposal = generate_initial_proposal()

print("Initial proposal:")
print(initial_proposal)

status = send_signed_message(
    private_key,
    did,
    initial_proposal,
)

print("Initial message status:", status)

if status != 200:
    raise SystemExit("Could not start discussion.")


while True:
    try:
        messages = get_messages(since)

    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print("Read error:", e)
        time.sleep(2)
        continue

    for message_data in messages:
        if not isinstance(message_data, dict):
            continue

        try:
            seq = int(message_data.get("seq", 0))
        except (TypeError, ValueError):
            continue

        since = max(since, seq)

        if message_data.get("from") != CRITIC_DID:
            continue

        text = message_data.get("text", "")

        if not text:
            continue

        print()
        print("Verified critic message received:")
        print(text)

        is_final = replies_sent == MAX_REPLIES - 1

        reply = improve_proposal(
            text,
            final=is_final,
        )

        if is_final:
            reply = "FINAL: " + reply

        status = send_signed_message(
            private_key,
            did,
            reply,
        )

        print("Technocore status:", status)

        if status == 200:
            print("Research agent replied:")
            print(reply)

            replies_sent += 1

        if replies_sent >= MAX_REPLIES:
            print()
            print("Research phase complete.")
            print("FINAL proposal sent to judge.")
            raise SystemExit