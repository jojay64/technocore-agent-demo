// FLOP devnet probe: one bounded inference request through the devnet gateway.
//
// Run:   node flop_probe/probe.ts
// Needs: FLOP_PROBE_STATE_DIR and FLOP_API_KEY_FILE (see flop_probe/README.md).
//
// The probe never retries, never reads remote content as instructions, and
// counts every attempt against a fixed daily budget before sending anything.

import { randomUUID } from "node:crypto";
import { appendFile, chmod, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  ConfigError,
  buildRequestBody,
  journalRecord,
  loadConfig,
  parseApiKey,
  parseBudgetState,
  parseCompletion,
  reserveAttempt,
  type ParsedFailure,
  type ParsedReply,
  type ProbeConfig,
} from "./lib.ts";

export interface ProbeDeps {
  env: Record<string, string | undefined>;
  fetch: typeof fetch;
  now: () => Date;
  monotonicMs: () => number;
  randomUUID: () => string;
  platform: NodeJS.Platform;
  log: (line: string) => void;
}

export const EXIT_OK = 0;
export const EXIT_REQUEST_FAILED = 1;
export const EXIT_CONFIG = 2;
export const EXIT_BUDGET_SPENT = 3;

const STATE_FILE = "budget.json";
const JOURNAL_FILE = "probe_journal.jsonl";

async function readOptional(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch (error: any) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function writeAtomic(path: string, content: string): Promise<void> {
  const tmp = `${path}.tmp`;
  await writeFile(tmp, content, { encoding: "utf8", mode: 0o600 });
  await rename(tmp, path);
}

async function readApiKey(path: string, platform: NodeJS.Platform): Promise<string> {
  if (platform !== "win32") {
    const info = await stat(path);
    if ((info.mode & 0o077) !== 0) {
      throw new ConfigError("FLOP_API_KEY_FILE must not be readable by group or others (chmod 600)");
    }
  }
  return parseApiKey(await readFile(path, "utf8"));
}

async function sendRequest(
  config: ProbeConfig,
  apiKey: string,
  requestId: string,
  deps: ProbeDeps,
): Promise<ParsedReply | ParsedFailure> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await deps.fetch(`${config.gatewayUrl}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${apiKey}`,
        "idempotency-key": requestId,
      },
      body: buildRequestBody(config),
      redirect: "error",
      signal: controller.signal,
    });
    // Bound the body we are willing to read.
    const text = (await response.text()).slice(0, 64_000);
    return parseCompletion(response.status, text);
  } catch (error: any) {
    return { ok: false, reason: error?.name === "AbortError" ? "timeout" : "network_error" };
  } finally {
    clearTimeout(timer);
  }
}

export async function runProbe(deps: ProbeDeps): Promise<number> {
  let config: ProbeConfig;
  let apiKey: string;
  try {
    config = loadConfig(deps.env);
    await mkdir(config.stateDir, { recursive: true, mode: 0o700 });
    apiKey = await readApiKey(config.apiKeyFile, deps.platform);
  } catch (error: any) {
    deps.log(`config error: ${error instanceof ConfigError ? error.message : error?.code ?? "unreadable file"}`);
    return EXIT_CONFIG;
  }

  const statePath = join(config.stateDir, STATE_FILE);
  const startedAt = deps.now();
  const state = parseBudgetState(await readOptional(statePath), startedAt);
  const reserved = reserveAttempt(state, config.maxRequestsPerDay);
  if (reserved === null) {
    deps.log(`budget spent for ${state.day} (max ${config.maxRequestsPerDay}); nothing sent`);
    return EXIT_BUDGET_SPENT;
  }
  // Persist the reservation before any network call, so a crash cannot
  // hand out the same attempt twice.
  await writeAtomic(statePath, JSON.stringify(reserved) + "\n");

  const requestId = deps.randomUUID();
  const t0 = deps.monotonicMs();
  const result = await sendRequest(config, apiKey, requestId, deps);
  const latencyMs = deps.monotonicMs() - t0;

  const record = journalRecord(config, reserved.attempts, requestId, startedAt, latencyMs, result);
  const journalPath = join(config.stateDir, JOURNAL_FILE);
  await appendFile(journalPath, JSON.stringify(record) + "\n", { encoding: "utf8", mode: 0o600 });
  if (deps.platform !== "win32") await chmod(journalPath, 0o600);

  deps.log(
    result.ok
      ? `ok attempt ${reserved.attempts}/${config.maxRequestsPerDay} in ${record.latency_ms} ms` +
          ` (request ${requestId}, settlement ${record.settlement_status ?? "unknown"})`
      : `failed attempt ${reserved.attempts}/${config.maxRequestsPerDay}: ${result.reason}`,
  );
  return result.ok ? EXIT_OK : EXIT_REQUEST_FAILED;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const code = await runProbe({
    env: process.env,
    fetch: globalThis.fetch,
    now: () => new Date(),
    monotonicMs: () => performance.now(),
    randomUUID: () => randomUUID(),
    platform: process.platform,
    log: (line) => console.log(line),
  });
  process.exitCode = code;
}
