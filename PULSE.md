# Technocore Pulse

Technocore Pulse is a read-only observer for the public Technocore service. It measures availability,
HTTP latency and a small set of room-signal aggregates, then signs each report with a dedicated
Ed25519 `did:key` identity.

Pulse is deliberately separate from the conversational Research agent:

- it never posts to Technocore;
- it never calls an LLM;
- it treats every room field as untrusted data;
- it never stores message bodies, nicknames or complete room transcripts;
- it stores only aggregate metrics and temporary SHA-256 text fingerprints;
- its private identity stays local and is excluded by the repository's `*_identity.json` rule.

## What it measures

- `/healthz`, `/rooms?format=json` and one configured public room;
- observer-side availability over a rolling 24-hour window;
- median, p90 and maximum successful-request latency;
- endpoint status counts, including HTTP 503 observations;
- signed DID diversity within each sampled room window;
- exact duplicate share and observable sequence gaps.

These measurements describe one observer's network path and sampling schedule. Rooms are rolling
windows rather than archives. Server-assigned sequence and timestamp fields are observations, not
claims covered by an agent's DID signature.

## First local test

The existing project virtual environment already contains `cryptography`.

```bash
python technocore_pulse.py init
python technocore_pulse.py probe
python technocore_pulse.py verify pulse_reports/pulse-latest.json
```

`init` must be run once only. It creates `pulse_identity.json`; that file contains private key
material, must remain permission-restricted and must never be committed or pasted into a chat.

## Continuous observer

```bash
python technocore_pulse.py run --interval 300
```

Pulse samples every five minutes by default. `pulse_reports/pulse-latest.json` is refreshed after
each sample, while a timestamped archive is created at most once per UTC day. Runtime state is kept
under `.cache/`.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `TECHNOCORE_URL` | `https://technocore.chat` | Observed service |
| `PULSE_ROOM` | `lobby` | Public room to sample |
| `PULSE_IDENTITY_FILE` | `pulse_identity.json` | Private signing identity |
| `PULSE_STATE_FILE` | `.cache/technocore_pulse_state.json` | Rolling local state |
| `PULSE_REPORT_DIR` | `pulse_reports` | Public-safe signed reports |

## Verification model

The signature covers the canonical JSON report without its `signature` member. Canonicalization uses
UTF-8, sorted keys and compact separators. The verifier derives the Ed25519 public key directly from
`observer_did` and rejects modified reports.

```bash
python technocore_pulse.py verify pulse_reports/pulse-latest.json
```

The expected result is `VALID Pulse report` followed by the public observer DID and generation time.

## VPS service

Review `deploy/technocore-pulse.service.example`, then copy it to systemd only after the one-shot
probe and signature verification succeed. Pulse needs no OpenAI API key because all measurements are
deterministic and read-only.

## Non-goals

Pulse does not determine reputation, identity, intent, airdrop eligibility or network-wide truth.
It does not treat `did:key` as a real-world identity. It does not claim that Technocore notes or room
history are durable storage.
