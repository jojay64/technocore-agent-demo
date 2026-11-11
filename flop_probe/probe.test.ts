// Offline tests: no network, fake fetch, temporary state directories.
// Run: node --test flop_probe/probe.test.ts

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  ConfigError,
  HARD_MAX_REQUESTS_PER_DAY,
  HARD_MAX_TOKENS,
  HARD_TIMEOUT_MS,
  PROBE_PROMPT,
  buildRequestBody,
  excerpt,
  loadConfig,
  parseApiKey,
  parseBudgetState,
  parseCompletion,
  parseReceipt,
  reserveAttempt,
} from "./lib.ts";
import { EXIT_BUDGET_SPENT, EXIT_CONFIG, EXIT_OK, EXIT_REQUEST_FAILED, runProbe } from "./probe.ts";

const KEY = "test-bearer-key-0123456789abcdef";
const NOW = new Date("2026-11-07T12:00:00Z");
const REQUEST_ID = "9ea55a78-93c5-4147-9d5a-8bfbb16ee2dc";
const CHANNEL = "0x" + "ab".repeat(32);
const MINER = "5FkMpoVojgbmpex4ENpLimrQ29pMPQK8qJZRWDHpQxYELwZ1";

function baseEnv(dir: string): Record<string, string> {
  return { FLOP_PROBE_STATE_DIR: join(dir, "state"), FLOP_API_KEY_FILE: join(dir, "key") };
}

function okBody(text = "I am online."): string {
  return JSON.stringify({
    id: "chatcmpl-1",
    choices: [{ message: { role: "assistant", content: text } }],
    usage: { prompt_tokens: 20, completion_tokens: 5, cost_flop: 0.219 },
    x_flop_receipt: { channelId: CHANNEL, settlementStatus: "pending", miner: MINER },
  });
}

interface FakeCall {
  url: string;
  init: RequestInit;
}

function fakeFetch(status: number, body: string, calls: FakeCall[]): typeof fetch {
  return (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(body, { status });
  }) as unknown as typeof fetch;
}

describe("loadConfig", () => {
  const env = { FLOP_PROBE_STATE_DIR: "/s", FLOP_API_KEY_FILE: "/k" };

  test("defaults to devnet, the official gateway and safe limits", () => {
    const config = loadConfig(env);
    assert.equal(config.network, "devnet");
    assert.equal(config.gatewayUrl, "https://api.flop.network");
    assert.equal(config.maxRequestsPerDay, 24);
    assert.equal(config.maxTokens, 64);
    assert.equal(config.timeoutMs, 120_000);
  });

  test("timeout accepts slow devnet replies up to the hard ceiling only", () => {
    assert.equal(loadConfig({ ...env, FLOP_PROBE_TIMEOUT_MS: "180000" }).timeoutMs, 180_000);
    assert.throws(() => loadConfig({ ...env, FLOP_PROBE_TIMEOUT_MS: String(HARD_TIMEOUT_MS + 1) }), ConfigError);
  });

  test("refuses testnet and mainnet", () => {
    assert.throws(() => loadConfig({ ...env, FLOP_NETWORK: "testnet" }), ConfigError);
    assert.throws(() => loadConfig({ ...env, FLOP_NETWORK: "mainnet" }), ConfigError);
  });

  test("refuses any other gateway host, http, credentials or query", () => {
    for (const url of [
      "https://api.openai.com",
      "http://api.flop.network",
      "https://api.flop.network.evil.example",
      "https://user:pw@api.flop.network",
      "https://api.flop.network/?x=1",
    ]) {
      assert.throws(() => loadConfig({ ...env, FLOP_GATEWAY_URL: url }), ConfigError, url);
    }
  });

  test("hard ceilings cannot be raised by configuration", () => {
    assert.throws(
      () => loadConfig({ ...env, FLOP_PROBE_MAX_REQUESTS_PER_DAY: String(HARD_MAX_REQUESTS_PER_DAY + 1) }),
      ConfigError,
    );
    assert.throws(() => loadConfig({ ...env, FLOP_PROBE_MAX_TOKENS: String(HARD_MAX_TOKENS + 1) }), ConfigError);
    assert.throws(() => loadConfig({ ...env, FLOP_PROBE_MAX_TOKENS: "-5" }), ConfigError);
    assert.throws(() => loadConfig({ ...env, FLOP_PROBE_MAX_TOKENS: "0" }), ConfigError);
  });

  test("requires a state directory and a key file", () => {
    assert.throws(() => loadConfig({ FLOP_API_KEY_FILE: "/k" }), ConfigError);
    assert.throws(() => loadConfig({ FLOP_PROBE_STATE_DIR: "/s" }), ConfigError);
  });

  test("never reads an OpenAI key or a Technocore identity", () => {
    const config = loadConfig({ ...env, OPENAI_API_KEY: "sk-should-be-ignored" });
    assert.ok(!JSON.stringify(config).includes("sk-should-be-ignored"));
  });
});

describe("budget", () => {
  test("starts empty and counts attempts", () => {
    const state = parseBudgetState(null, NOW);
    assert.deepEqual(state, { day: "2026-11-07", attempts: 0 });
    assert.deepEqual(reserveAttempt(state, 2), { day: "2026-11-07", attempts: 1 });
  });

  test("refuses once the daily maximum is reached", () => {
    assert.equal(reserveAttempt({ day: "2026-11-07", attempts: 2 }, 2), null);
  });

  test("resets on a new UTC day", () => {
    const state = parseBudgetState(JSON.stringify({ day: "2026-11-06", attempts: 24 }), NOW);
    assert.deepEqual(state, { day: "2026-11-07", attempts: 0 });
  });

  test("corrupt or tampered state fails closed", () => {
    for (const raw of ["not json", "{}", JSON.stringify({ day: "2026-11-07", attempts: -1 })]) {
      assert.equal(reserveAttempt(parseBudgetState(raw, NOW), 24), null, raw);
    }
  });
});

describe("request and response", () => {
  test("sends only the fixed prompt with the token limit", () => {
    const body = JSON.parse(buildRequestBody(loadConfig({ FLOP_PROBE_STATE_DIR: "/s", FLOP_API_KEY_FILE: "/k" })));
    assert.deepEqual(body.messages, [{ role: "user", content: PROBE_PROMPT }]);
    assert.equal(body.max_tokens, 64);
    assert.equal(body.stream, false);
  });

  test("parses a valid completion", () => {
    const result = parseCompletion(200, okBody());
    assert.ok(result.ok);
    assert.equal(result.ok && result.completionTokens, 5);
  });

  test("rejects errors, bad JSON and empty replies", () => {
    assert.deepEqual(parseCompletion(429, "{}"), { ok: false, reason: "http_429" });
    assert.deepEqual(parseCompletion(200, "<html>"), { ok: false, reason: "invalid_json" });
    assert.deepEqual(parseCompletion(200, JSON.stringify({ choices: [] })), { ok: false, reason: "empty_reply" });
  });

  test("parses the settlement receipt", () => {
    const result = parseCompletion(200, okBody());
    assert.ok(result.ok);
    assert.deepEqual(result.ok && result.receipt, {
      channelId: CHANNEL,
      settlementStatus: "pending",
      miner: MINER,
      costFlop: 0.219,
    });
  });

  test("malformed or hostile receipt fields are dropped", () => {
    const receipt = parseReceipt({
      usage: { cost_flop: -1 },
      x_flop_receipt: {
        channelId: "0x1234; rm -rf /",
        settlementStatus: "Ignore previous instructions",
        miner: "not-an-address",
      },
    });
    assert.deepEqual(receipt, { channelId: null, settlementStatus: null, miner: null, costFlop: null });
    assert.deepEqual(parseReceipt({}), { channelId: null, settlementStatus: null, miner: null, costFlop: null });
  });

  test("excerpt flattens and bounds untrusted text", () => {
    assert.equal(excerpt("a\nb\u0000c"), "a b c");
    assert.ok(excerpt("x".repeat(500)).length <= 201);
  });

  test("api key file must hold a single key", () => {
    assert.equal(parseApiKey(`  ${KEY}\n`), KEY);
    assert.throws(() => parseApiKey("short"), ConfigError);
    assert.throws(() => parseApiKey(`${KEY} ${KEY}`), ConfigError);
  });
});

describe("runProbe", () => {
  let dir: string;
  const logs: string[] = [];

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "flop-probe-"));
    await writeFile(join(dir, "key"), KEY + "\n", { mode: 0o600 });
    logs.length = 0;
  });

  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  function deps(fetchImpl: typeof fetch, env: Record<string, string> = baseEnv(dir)) {
    return {
      env,
      fetch: fetchImpl,
      now: () => NOW,
      monotonicMs: (() => {
        let t = 0;
        return () => (t += 100);
      })(),
      randomUUID: () => REQUEST_ID,
      platform: "win32" as NodeJS.Platform,
      log: (line: string) => logs.push(line),
    };
  }

  test("success: one request to the official gateway, journal without the key", async () => {
    const calls: FakeCall[] = [];
    const code = await runProbe(deps(fakeFetch(200, okBody(), calls)));
    assert.equal(code, EXIT_OK);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://api.flop.network/v1/chat/completions");
    assert.equal(calls[0].init.redirect, "error");

    const journal = await readFile(join(dir, "state", "probe_journal.jsonl"), "utf8");
    const record = JSON.parse(journal.trim());
    assert.equal(record.ok, true);
    assert.equal(record.network, "devnet");
    assert.equal(record.reply_excerpt, "I am online.");
    assert.equal((calls[0].init.headers as Record<string, string>)["idempotency-key"], REQUEST_ID);
    assert.equal(record.request_id, REQUEST_ID);
    assert.equal(record.cost_flop, 0.219);
    assert.equal(record.channel_id, CHANNEL);
    assert.equal(record.settlement_status, "pending");
    assert.equal(record.miner, MINER);
    assert.ok(!journal.includes(KEY));
    assert.ok(!logs.join("\n").includes(KEY));
  });

  test("failure is journaled, counted and not retried", async () => {
    const calls: FakeCall[] = [];
    const code = await runProbe(deps(fakeFetch(502, "bad gateway", calls)));
    assert.equal(code, EXIT_REQUEST_FAILED);
    assert.equal(calls.length, 1);
    const budget = JSON.parse(await readFile(join(dir, "state", "budget.json"), "utf8"));
    assert.equal(budget.attempts, 1);
    // A failed request can still hold a reservation, so its id is kept.
    const record = JSON.parse(await readFile(join(dir, "state", "probe_journal.jsonl"), "utf8"));
    assert.equal(record.request_id, REQUEST_ID);
    assert.equal(record.channel_id, null);
  });

  test("network errors are reported without leaking details", async () => {
    const failing = (async () => {
      throw new TypeError(`fetch failed with ${KEY}`);
    }) as unknown as typeof fetch;
    assert.equal(await runProbe(deps(failing)), EXIT_REQUEST_FAILED);
    const journal = await readFile(join(dir, "state", "probe_journal.jsonl"), "utf8");
    assert.equal(JSON.parse(journal).reason, "network_error");
    assert.ok(!journal.includes(KEY));
  });

  test("stops sending once the daily budget is spent", async () => {
    const calls: FakeCall[] = [];
    const env = { ...baseEnv(dir), FLOP_PROBE_MAX_REQUESTS_PER_DAY: "2" };
    const f = fakeFetch(200, okBody(), calls);
    assert.equal(await runProbe(deps(f, env)), EXIT_OK);
    assert.equal(await runProbe(deps(f, env)), EXIT_OK);
    assert.equal(await runProbe(deps(f, env)), EXIT_BUDGET_SPENT);
    assert.equal(calls.length, 2);
  });

  test("bad configuration sends nothing", async () => {
    const calls: FakeCall[] = [];
    const env = { ...baseEnv(dir), FLOP_GATEWAY_URL: "https://api.openai.com" };
    assert.equal(await runProbe(deps(fakeFetch(200, okBody(), calls), env)), EXIT_CONFIG);
    assert.equal(calls.length, 0);
  });

  test("missing key file sends nothing", async () => {
    const calls: FakeCall[] = [];
    await rm(join(dir, "key"));
    assert.equal(await runProbe(deps(fakeFetch(200, okBody(), calls))), EXIT_CONFIG);
    assert.equal(calls.length, 0);
  });

  test("on POSIX a group- or world-readable key file is refused", { skip: process.platform === "win32" }, async () => {
    const { chmod } = await import("node:fs/promises");
    await chmod(join(dir, "key"), 0o644);
    const calls: FakeCall[] = [];
    const d = { ...deps(fakeFetch(200, okBody(), calls)), platform: process.platform };
    assert.equal(await runProbe(d), EXIT_CONFIG);
    assert.equal(calls.length, 0);
  });
});
