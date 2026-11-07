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
| `FLOP_PROBE_TIMEOUT_MS` | no | `30000` (max 60000) |

## Run

```sh
FLOP_PROBE_STATE_DIR=~/flop-devnet/probe FLOP_API_KEY_FILE=~/flop-devnet/gateway.key node flop_probe/probe.ts
```

Exit codes: `0` success, `1` request failed, `2` configuration error (nothing
sent), `3` daily budget spent (nothing sent).

The state directory holds `budget.json` (today's attempt count) and
`probe_journal.jsonl` (one record per attempt: time, latency, token counts,
response id and a reply excerpt).

## Tests

Offline only, no network:

```sh
node --test flop_probe/probe.test.ts
```

## Status

Not deployed. The devnet gateway enrollment (`flop agent credential`) is
currently failing on the FLOP side (flop-labs/network#136), so no key exists yet.
