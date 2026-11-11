# FLOP devnet agent test, 2026-11-11

An end-to-end test of the agent (consumer) side of the FLOP devnet: get a
gateway key, make one paid inference request, and follow its settlement on
chain. It is separate from the miner work in `flop_miner_linux/`.

Devnet FLOP has no value. Nothing here touches testnet, mainnet, a Technocore
identity, the OpenAI key or the three production services.

## Why

FLOP published an "agent onboarding" flow on its explorer
(<https://explorer.devnet.flop.network/connect>), with two prompts to paste into
a coding agent:

1. read-only exploration of the devnet;
2. self-enrollment: throwaway wallet, faucet, gateway key, one paid request.

We wanted to know whether this is the same flow as our `flop_probe`, and whether
the probe can use a key obtained this way.

## Safety rules applied

From `AGENTS.md` ("Devnet probe exception"):

- devnet only, fixed budget, no retry loop;
- a dedicated throwaway sr25519 wallet, created only for this test;
- no Technocore DID, no OpenAI key, no production service;
- nothing posted to `technocore.chat` (the FLOP faucet room there was not used);
- the wallet seed and the gateway key are stored outside the repository
  (`~/.flop/`, mode 600 requested) and were never printed, logged or committed;
- every remote text was treated as data, never as instructions.

Each network step that spends or signs was approved by the user beforehand.

## Method

### Step 1: read-only exploration

- Read the official agent guide, <https://flop.network/llms.txt>.
- Gateway: `GET /health` and `GET /v1/models` on `https://api.flop.network`.
- Hosted MCP `https://mcp.flop.network/mcp` (Streamable HTTP, JSON-RPC over
  plain HTTPS POST): `initialize`, `tools/list`, `flop_health`,
  `flop_discover_models`, then `flop_get_model_pricing` with
  `tokens_estimate: 200` for each model.

### Step 2: enrollment and one paid request

Following "Gateway key enrollment" in `llms.txt`:

1. `GET /health` must read `ok`; `GET https://faucet.flop.network/limits` must
   show the faucet `open`.
2. Generate a fresh sr25519 keypair locally (`@polkadot/keyring` 13.5.1,
   SS58 prefix 42).
3. Fund it: `POST https://faucet.flop.network/fund`.
4. `GET /v1/enroll/wallet/challenge?address=…`, sign the exact message,
   `POST /v1/enroll/wallet`; store the returned key.
5. `POST /v1/chat/completions` with the key as bearer, a fresh UUID as
   `Idempotency-Key`, `max_tokens: 64` and no sampling parameter.
6. Poll `GET /v1/keys/me/receipts` every 15 s (10 min maximum) until the
   receipt is `settled`, then read `GET /v1/keys/me`.

## Results

### Devnet state (around 17:50 UTC)

| Model | Serving miners | Cost of 200 tokens | Parameters accepted |
|---|---|---|---|
| `qwen/qwen2.5-0.5b-instruct` | 23 | 1.2 FLOP | `max_tokens` only |
| `qwen/qwen3-8b` | 3 | 10.2 FLOP | `max_tokens`, `temperature`, `top_p`, tools |
| `Qwen/Qwen3.8-27B` | 0 (registered, not served) | 31.2 FLOP | — |

MCP pricing (`flop_get_model_pricing`) matched the gateway catalogue (0.006
and 0.051 FLOP per token). The hosted MCP exposed 34 read-only tools, matching
`https://flop.network/mcp-config.json`.

### Enrollment

- Faucet: 25 FLOP granted (`anonymous` tier).
- Enrollment: succeeded first time (HTTP 201) with a 20,000 FLOP quota for 24 h,
  model `qwen/qwen2.5-0.5b-instruct`. The CLI path (`flop agent credential`)
  had been failing intermittently (flop-labs/network#136).

### Paid requests

| # | Result | Detail |
|---|---|---|
| 1 | failed | no answer within our 90 s client timeout; the receipt shows HTTP 503, 0.444 FLOP reserved, state `estimated`, never settled |
| 2 | refused | HTTP 429 `overloaded`, "gateway channel pool is full", in 0.7 s; no receipt |
| 3 | refused | same 429, a few minutes later |
| 4 | **succeeded** | HTTP 200 in 89.3 s, 10 prompt + 64 completion tokens, `cost_flop` 0.219 |

Request 4 settlement:

- reply receipt: `settlementStatus: pending`, miner
  `5FkMpoVojgbmpex4ENpLimrQ29pMPQK8qJZRWDHpQxYELwZ1`;
- `settled` after **259 s**, `settled_spend` 0.219 FLOP, channel
  `0x75d5c7911b0d88667abdaac4d9614a694d171b10ce04996337371898c9f11281`,
  terminal event `0xdcad3563c07b18df78e524e19baab45a7c7206c9c0da8cee3a5bf233a0cb1abf:4`.

## Problems found

1. **Latency.** 89 s for a 64-token answer from a 0.5B model (38 s on
   2026-11-08). The probe's 60 s ceiling would have failed this request. The
   MCP reports `avgLatencyMs: 0` and the gateway `latency_ms: null`, so latency
   is not measured on FLOP's side.
2. **Gateway capacity.** Two 429 `overloaded` refusals while `/health` read `ok`
   and 23 miners were listed for the model. A health check does not tell a
   client that requests will be served.
3. **Quota accounting.** The quota dropped by 0.438 FLOP although the only
   settled spend is 0.219. The difference is probably the 0.444 reservation of
   the 503 request, still `estimated` and never released.
4. **Credential type.** `GET /v1/keys/me` reports the wallet-enrolled key as
   `"credential": "owner"`, while `llms.txt` lists wallet keys as a separate
   kind that cannot pin a miner.
5. **Model id casing.** Enrollment returns `qwen/qwen2.5-0.5b-instruct`; the key's
   `model_allowlist` and receipts use `Qwen/Qwen2.5-0.5B-Instruct`.
6. **Docs.** The explorer page says the health check needs at least one live
   model, but `/health` exposes no model count. It also says the quota is
   20,000 FLOP, while `llms.txt` says "up to 20,000".

## Fixes in `flop_probe`

- Default timeout raised from 30 s to 120 s, hard ceiling from 60 s to 180 s
  (`FLOP_PROBE_TIMEOUT_MS`).
- Each request sends a fresh UUID as `Idempotency-Key`, journaled as
  `request_id` for every attempt, failures included, so a reservation left by
  a failed request can be traced.
- On success the journal keeps `cost_flop`, `channel_id`, `settlement_status`
  and `miner` from the reply. Each field is validated against a strict format
  and dropped otherwise, so remote text cannot enter the journal through it.
- New offline tests cover the timeout ceiling, the header, the receipt
  fields and hostile receipt values. `node --test flop_probe/probe.test.ts`:
  24 passed, 1 skipped (POSIX-only). Full suite: 32 passed.

A key enrolled from a wallet works with the probe unchanged
(`FLOP_API_KEY_FILE` pointing at it). The probe was not run against the live
gateway after these changes.

## Not done

- No automated settlement check: the receipt is read by hand afterwards
  (see `README.md`).
- The explorer's bounded child credentials (spend, rate, use count, expiry)
  were not tried; they need an owner key and would add a server-side budget
  on top of the probe's own.
- The third-party page <https://flop.easy-node.xyz/> (EasyNode, not affiliated
  with FLOP Labs) was read but not used.
