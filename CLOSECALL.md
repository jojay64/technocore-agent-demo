# Close Call preparation — offline protocol only

This step adds `closecall_protocol.py`, a read-only `closecall_observer.py`,
their offline tests, and an example service unit. Nothing has been installed,
started, signed, registered or traded.
The three existing services and their identity files remain unchanged.

## Evidence and trust boundary

The 25 September 2026 audit checked the seed in `d-close1-price`, generation 1,
sequence 1, nonce `1790337922535`, server timestamp `2026-09-25T12:05:22.575364Z`.
Its Ed25519 signature verifies under:

`did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte`

This is an **observed signer**, not independently authenticated as FLOP Labs.
An official launch announcement binding that DID is still required. A valid
signature, owned room or correct package hash alone does not supply that binding.
Server sequence, timestamp and generation are not covered by the signature.

The seed pins manifest SHA-256:

`bae09812e25eb6f1369c611f24964f7ea0acafddfc45301a16f33f941296dafa`

It matches the official repository at commit
`66c1da36538e4b1c685417d2f66922906b13fea0`, package `0.1.0-draft`,
rules `0.1-draft`. All 15 artifact hashes were checked during the audit.
This implementation is pinned to that observation; it never discovers or trusts
new rules from room text or automatically updates its pins.

Sources:

- https://github.com/flop-labs/technocore-close-call-challenge/tree/66c1da36538e4b1c685417d2f66922906b13fea0
- https://github.com/flop-labs/technocore-close-call-challenge/pull/4
- https://technocore.chat/

## What is validated

- Exact Ed25519 transport signatures and canonical base64url encoding.
- Ed25519 DID encoding, positive decimal nonces of at most 19 digits, and
  unchanged single-line transport bytes.
- Bounded JSON, duplicate-key and non-finite-number rejection.
- Owner messages naming the transport signer and the `close-1` season.
- Exact trade fields, decimal string amounts, minimum quantity, canonical
  sorted terms and both domain-separated maker/taker signatures.
- A trade's publisher must be its maker or countersigner; a named taker must match.
- The audited seed signer, content pins and package manifest hash.

Only `close1` participant messages and the five named referee rooms are allowed
at the transport layer. Owner/trade messages are accepted only in `close1`.
Additional registered rooms and informal offer conventions are deliberately
unsupported. No payment, tclk lifecycle, or real asset support is introduced.

The unsigned `owner_text()` helper uses the historical Research DID by default:

`did:key:z6MkkPtvJEneCieb8AVphWVuEcxihMs2BK9HCETMtRQjFuAv`

It does not load keys, generate an identity, allocate a nonce or sign anything.
No private identity file is required even to import this module.

## Deliberately not established by this step

Passing validation means only that a message is authentic and structurally valid.
It does **not** prove registration, sufficient POLF, price-band compliance,
freshness, replay safety, acceptance, settlement or an official referee identity.
Trade `until` is checked for shape only; expiry and the lock require sweep state.
Transport timestamps are parsed, not trusted as signed evidence of time.

The observer reads only the six pinned rooms. It archives raw records, tracks
room generations and cursors, catches up through bounded pages, and stops on
sequence gaps or generation changes. Its first read is only the latest bounded
API window, marked `tail_only`; it cannot establish a full transcript or ledger.
It verifies the referee signer and known room/message types. In `close1`, it
classifies messages as valid owner/trade, signed but unsupported, or unverified.
An unsupported or unauthenticated message is never treated as a challenge action.
The durable room cursor is committed after each transcript append. On a crash,
one record may be repeated; records are never silently skipped.

The observer has no DID file setting, key-loading code, signature generator,
POST request or non-GET endpoint. It does not use the existing tclk deduplication
cache as a replay ledger.

Then implement full sweep/ledger reconciliation before any trading evaluation.
Visible `flow.mints` can omit keys (`omitted.mints`); absence is not a negative
receipt. A hash without its full archive file cannot establish an owner's balance.
Keep credit unconfirmed until signed positive evidence can be reconciled. Never
trade to test whether a registration succeeded.

The future OBSERVE process must have no key-loading or signing capability.
Cosigning a trade is an externally usable commitment even without publishing it.
Any future active execution requires separate explicit authorization.

## Local use

One read-only pass (only after deciding to make live GET requests):

```powershell
.venv\Scripts\python.exe closecall/closecall_observer.py --once
```

Continuous local observation uses `--interval 30`. The example systemd unit is
not installed or started. It requires only network reads and local `.cache`
writes, but the repository's history remains incomplete until it can be
reconciled with a full archive. Do not use its tail data to trade.

## Validation

On Windows, run the new tests and then the full existing suite:

```powershell
.venv\Scripts\python.exe run_tests.py -v
.venv\Scripts\python.exe run_tests.py -v
git diff --check
```

On Linux use `.venv/bin/python` with the same arguments. Tests use temporary
fixture identities only, with network connections blocked for the new tests.
No service script needs to be started. No deployment or service commands are
provided in this phase.
