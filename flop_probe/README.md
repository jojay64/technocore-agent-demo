# FLOP devnet probe

A small, separate probe that sends **one bounded inference request** to the FLOP
devnet gateway and journals the result. It exists to exercise the agent side of
the FLOP devnet before the testnet opens.

It follows the "Devnet probe exception" in `AGENTS.md`:

- devnet only: `FLOP_NETWORK` other than `devnet` is refused;
- only `https://api.flop.network` is accepted as gateway;
- fixed budget: at most `FLOP_PROBE_MAX_REQUESTS_PER_DAY` attempts per UTC day
  (default 24, hard ceiling 48) and `FLOP_PROBE_MAX_TOKENS` per reply
  (default 64, hard ceiling 128);
- every attempt counts, failures included, and nothing is retried;
- one fixed prompt; remote text is stored only as a bounded one-line excerpt;
- no Technocore identity, no OpenAI key, no link to the three production services.

## Requirements

- Node.js 24 or later (runs the TypeScript files directly, no build step).
- A devnet gateway bearer key from `flop agent credential --print-key`, saved
  alone in a file with mode `600`. The key never appears in logs or journals.

## Configuration

| Variable | Required | Default |
|---|---|---|
| `FLOP_PROBE_STATE_DIR` | yes | |
| `FLOP_API_KEY_FILE` | yes | |
| `FLOP_NETWORK` | no | `devnet` (only accepted value) |
| `FLOP_GATEWAY_URL` | no | `https://api.flop.network` |
| `FLOP_PROBE_MODEL` | no | `qwen/qwen2.5-0.5b-instruct` |
| `FLOP_PROBE_MAX_REQUESTS_PER_DAY` | no | `24` (max 48) |
| `FLOP_PROBE_MAX_TOKENS` | no | `64` (max 128) |
| `FLOP_PROBE_TIMEOUT_MS` | no | `120000` (max 180000) |

## Run

Devnet completions are slow: 38.4 s on 2026-11-08 (flop-labs/network#136) and
89.3 s on 2026-11-11, which is why the default timeout is 120 s.

```sh
FLOP_PROBE_STATE_DIR=~/flop-devnet/probe FLOP_API_KEY_FILE=~/flop-devnet/gateway.key node flop_probe/probe.ts
```

Exit codes: `0` success, `1` request failed, `2` configuration error (nothing
sent), `3` daily budget spent (nothing sent).

The state directory holds `budget.json` (today's attempt count) and
`probe_journal.jsonl` (one record per attempt: time, latency, token counts,
response id, a reply excerpt and the settlement receipt).

## Settlement receipt

Each request carries a fresh UUID in an `Idempotency-Key` header, journaled as
`request_id`, failures included (a failed request can still hold a
reservation). On success the journal also keeps `cost_flop`, `channel_id`,
`settlement_status` and `miner` from the reply; malformed values are dropped.

The reply is usually `pending`; settlement took 259 s on 2026-11-11. To check
it later, read `GET https://api.flop.network/v1/keys/me/receipts` with the same
key and find the receipt whose `request_id` matches; it reads `settled` with a
`settled_spend` once done.

## Tests

Offline only, no network:

```sh
node --test flop_probe/probe.test.ts
```

## Status

Not deployed. The devnet gateway enrollment (`flop agent credential`) was
failing on the FLOP side (flop-labs/network#136) and succeeded on retry on
2026-11-08; it is intermittent, so a retry may be needed. Credentials are
short-lived (the 2026-11-08 one expired the same day).

A gateway key can also be enrolled without the CLI, from a funded sr25519
wallet ("Gateway key enrollment" in https://flop.network/llms.txt; 20,000 FLOP
quota for 24 h). On 2026-11-11 that worked first time; of four requests, one
returned 503, two 429 `overloaded` ("gateway channel pool is full") and one
succeeded and settled on chain.
