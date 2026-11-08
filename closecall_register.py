"""One-shot Close Call owner registration using the existing Research identity.

Run manually from the repository on the VPS. Never retries a write.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import closecall_protocol as protocol


IDENTITY_PATH = Path("research_identity.json")
URL = "https://technocore.chat/r/close1/say-signed"


def main():
    if not IDENTITY_PATH.is_file():
        raise SystemExit("STOP: research_identity.json missing; nothing sent")
    try:
        identity = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        if identity.get("did") != protocol.HISTORICAL_DID:
            raise ValueError
        private_hex = identity.get("private_key_hex")
        if not isinstance(private_hex, str) or len(private_hex) != 64:
            raise ValueError
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
        raw_public = key.public_key().public_bytes_raw()
        if protocol.public_key(protocol.HISTORICAL_DID).public_bytes_raw() != raw_public:
            raise ValueError
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit("STOP: historical identity invalid or mismatched; nothing sent")

    text = protocol.owner_text()
    nonce = str(time.time_ns())
    signature = base64.urlsafe_b64encode(
        key.sign(f"close1|{nonce}|{text}".encode("utf-8"))
    ).decode("ascii").rstrip("=")
    request = urllib.request.Request(
        "/".join((URL, protocol.HISTORICAL_DID, signature, nonce,
                   urllib.parse.quote(text, safe=""))),
        method="GET",
        headers={"Accept": "text/plain",
                 "User-Agent": "technocore-closecall-owner/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise SystemExit(f"STOP: server returned HTTP {response.status}; do not retry")
            response.read(65536)  # The API returns text/plain; do not assume a JSON receipt.
            print(f"WRITE_HTTP_OK {response.status}; verify close1 and the flow before retrying")
            return
    except urllib.error.HTTPError as error:
        raise SystemExit(
            f"STOP: server returned HTTP {error.code}; do not retry; verify close1 and the flow"
        )
    except Exception as error:
        raise SystemExit(
            f"STOP: write outcome may be unknown ({type(error).__name__}); inspect close1 before retry"
        )


if __name__ == "__main__":
    main()
