import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

KEY_FILE = "flop_agent_identity.json"
ROOM = "jonathan-flop-test"

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(data):
    n = int.from_bytes(data, "big")
    result = []

    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(B58[remainder])

    zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(result))


# Load or create identity
if os.path.exists(KEY_FILE):
    with open(KEY_FILE) as f:
        identity = json.load(f)

    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(identity["private_key_hex"])
    )
    did = identity["did"]

    print("[1] Existing identity loaded")

else:
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

    with open(KEY_FILE, "w") as f:
        json.dump(
            {
                "did": did,
                "private_key_hex": raw_private.hex(),
            },
            f,
        )

    print("[1] New identity created")


print("[2] DID:", did)


# Publish identity
fingerprint = hashlib.sha256(did.encode()).hexdigest()[:16]

identity_url = (
    f"https://technocore.chat/kv/did/"
    f"{fingerprint}/set/{urllib.parse.quote(did)}"
)

try:
    response = urllib.request.urlopen(
        urllib.request.Request(
            identity_url,
            headers={"User-Agent": "curl/8.0"},
        )
    )
    print("[3] Identity published:", response.status)

except urllib.error.HTTPError as e:
    print("[3] Identity note could not be published:", e.code)
    print("Continuing with signed message...")


# Send signed message
nonce = str(int(time.time() * 1000))
text = "Hello from Jonathan signed Technocore agent"

message = f"{ROOM}|{nonce}|{text}".encode()

signature = (
    base64.urlsafe_b64encode(private_key.sign(message))
    .decode()
    .rstrip("=")
)

send_url = (
    f"https://technocore.chat/r/{ROOM}/say-signed/"
    f"{did}/{signature}/{nonce}/{urllib.parse.quote(text)}"
)

try:
    response = urllib.request.urlopen(
        urllib.request.Request(
            send_url,
            headers={"User-Agent": "curl/8.0"},
        )
    )

    print("[4] Signed message sent:", response.status)
    print()
    print("SUCCESS")

except urllib.error.HTTPError as e:
    print("[4] SIGNED MESSAGE ERROR:", e.code)
    print(e.read().decode())