// FLOP devnet probe: pure logic, no I/O.
//
// Scope (AGENTS.md "Devnet probe exception"): devnet test tokens only, a
// fixed budget, no Technocore identity, no OpenAI key, no production service.

export const DEVNET_GATEWAY = "https://api.flop.network";
export const ALLOWED_GATEWAY_HOSTS: readonly string[] = ["api.flop.network"];
export const DEFAULT_MODEL = "qwen/qwen2.5-0.5b-instruct";

// Hard ceilings: configuration can lower these, never raise them.
export const HARD_MAX_REQUESTS_PER_DAY = 48;
export const HARD_MAX_TOKENS = 128;
// Devnet completions took 38 s (2026-11-08) and 89 s (2026-11-11).
export const HARD_TIMEOUT_MS = 180_000;
export const DEFAULT_TIMEOUT_MS = 120_000;

// The probe sends one fixed prompt. Nothing remote is ever turned into a prompt.
export const PROBE_PROMPT =
  "Reply with exactly one short sentence confirming that you are online.";

export const MAX_EXCERPT_CHARS = 200;

export interface ProbeConfig {
  network: "devnet";
  gatewayUrl: string;
  model: string;
  maxRequestsPerDay: number;
  maxTokens: number;
  timeoutMs: number;
  stateDir: string;
  apiKeyFile: string;
}

export class ConfigError extends Error {}

function intInRange(raw: string | undefined, fallback: number, max: number, name: string): number {
  if (raw === undefined || raw === "") return fallback;
  if (!/^\d+$/.test(raw)) throw new ConfigError(`${name} must be a positive integer`);
  const value = Number(raw);
  if (value < 1) throw new ConfigError(`${name} must be at least 1`);
  if (value > max) throw new ConfigError(`${name} must not exceed ${max}`);
  return value;
}

export function loadConfig(env: Record<string, string | undefined>): ProbeConfig {
  const network = env.FLOP_NETWORK ?? "devnet";
  if (network !== "devnet") {
    throw new ConfigError("FLOP_NETWORK must be devnet; testnet and mainnet are out of scope");
  }

  const gatewayUrl = (env.FLOP_GATEWAY_URL ?? DEVNET_GATEWAY).replace(/\/+$/, "");
  let parsed: URL;
  try {
    parsed = new URL(gatewayUrl);
  } catch {
    throw new ConfigError("FLOP_GATEWAY_URL is not a valid URL");
  }
  if (parsed.protocol !== "https:") throw new ConfigError("FLOP_GATEWAY_URL must use https");
  if (!ALLOWED_GATEWAY_HOSTS.includes(parsed.hostname)) {
    throw new ConfigError(`FLOP_GATEWAY_URL host must be one of: ${ALLOWED_GATEWAY_HOSTS.join(", ")}`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new ConfigError("FLOP_GATEWAY_URL must not carry credentials, a query or a fragment");
  }

  const model = env.FLOP_PROBE_MODEL ?? DEFAULT_MODEL;
  if (!/^[a-z0-9][a-z0-9._/-]{0,127}$/i.test(model)) {
    throw new ConfigError("FLOP_PROBE_MODEL has an unexpected format");
  }

  const stateDir = env.FLOP_PROBE_STATE_DIR;
  if (!stateDir) throw new ConfigError("FLOP_PROBE_STATE_DIR is required");

  const apiKeyFile = env.FLOP_API_KEY_FILE;
  if (!apiKeyFile) throw new ConfigError("FLOP_API_KEY_FILE is required");

  return {
    network: "devnet",
    gatewayUrl: parsed.origin,
    model,
    maxRequestsPerDay: intInRange(env.FLOP_PROBE_MAX_REQUESTS_PER_DAY, 24, HARD_MAX_REQUESTS_PER_DAY, "FLOP_PROBE_MAX_REQUESTS_PER_DAY"),
    maxTokens: intInRange(env.FLOP_PROBE_MAX_TOKENS, 64, HARD_MAX_TOKENS, "FLOP_PROBE_MAX_TOKENS"),
    timeoutMs: intInRange(env.FLOP_PROBE_TIMEOUT_MS, DEFAULT_TIMEOUT_MS, HARD_TIMEOUT_MS, "FLOP_PROBE_TIMEOUT_MS"),
    stateDir,
    apiKeyFile,
  };
}

export function parseApiKey(raw: string): string {
  const key = raw.trim();
  if (key.length < 16 || key.length > 512 || /\s/.test(key)) {
    throw new ConfigError("API key file must contain exactly one bearer key");
  }
  return key;
}

// ---------------------------------------------------------------------------
// Daily budget. Every attempt counts, including failures, so a broken gateway
// cannot turn the probe into a retry loop.
// ---------------------------------------------------------------------------

export interface BudgetState {
  day: string; // UTC date, YYYY-MM-DD
  attempts: number;
}

export function utcDay(now: Date): string {
  return now.toISOString().slice(0, 10);
}

export function parseBudgetState(raw: string | null, now: Date): BudgetState {
  const today = utcDay(now);
  if (raw === null) return { day: today, attempts: 0 };
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    // Unreadable state fails closed: treat today's budget as spent.
    return { day: today, attempts: Number.MAX_SAFE_INTEGER };
  }
  const state = data as Partial<BudgetState>;
  if (typeof state.day !== "string" || !Number.isInteger(state.attempts) || (state.attempts as number) < 0) {
    return { day: today, attempts: Number.MAX_SAFE_INTEGER };
  }
  if (state.day !== today) return { day: today, attempts: 0 };
  return { day: state.day, attempts: state.attempts as number };
}

export function reserveAttempt(state: BudgetState, maxPerDay: number): BudgetState | null {
  if (state.attempts >= maxPerDay) return null;
  return { day: state.day, attempts: state.attempts + 1 };
}

// ---------------------------------------------------------------------------
// Request and response
// ---------------------------------------------------------------------------

export function buildRequestBody(config: ProbeConfig): string {
  return JSON.stringify({
    model: config.model,
    messages: [{ role: "user", content: PROBE_PROMPT }],
    max_tokens: config.maxTokens,
    stream: false,
  });
}

// Settlement receipt returned with a paid completion (x_flop_receipt). Every
// field is validated against a strict format and dropped otherwise, so remote
// text never reaches the journal through it.
export interface Receipt {
  channelId: string | null;
  settlementStatus: string | null;
  miner: string | null;
  costFlop: number | null;
}

export interface ParsedReply {
  ok: true;
  text: string;
  promptTokens: number | null;
  completionTokens: number | null;
  responseId: string | null;
  receipt: Receipt;
}

function matching(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

export function parseReceipt(body: any): Receipt {
  const receipt = body?.x_flop_receipt;
  const cost = body?.usage?.cost_flop;
  return {
    channelId: matching(receipt?.channelId, /^0x[0-9a-f]{64}$/i),
    settlementStatus: matching(receipt?.settlementStatus, /^[a-z_]{1,32}$/),
    miner: matching(receipt?.miner, /^[1-9A-HJ-NP-Za-km-z]{46,48}$/),
    costFlop: typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? cost : null,
  };
}

export interface ParsedFailure {
  ok: false;
  reason: string;
}

function tokenCount(value: unknown): number | null {
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : null;
}

export function parseCompletion(status: number, bodyText: string): ParsedReply | ParsedFailure {
  if (status !== 200) return { ok: false, reason: `http_${status}` };
  let body: any;
  try {
    body = JSON.parse(bodyText);
  } catch {
    return { ok: false, reason: "invalid_json" };
  }
  const text = body?.choices?.[0]?.message?.content;
  if (typeof text !== "string" || text.trim() === "") return { ok: false, reason: "empty_reply" };
  return {
    ok: true,
    text,
    promptTokens: tokenCount(body?.usage?.prompt_tokens),
    completionTokens: tokenCount(body?.usage?.completion_tokens),
    responseId: typeof body?.id === "string" ? body.id.slice(0, 128) : null,
    receipt: parseReceipt(body),
  };
}

// ---------------------------------------------------------------------------
// Journal. Remote text is untrusted data: it is trimmed, flattened to one line
// and stored only as an excerpt. The API key never enters a record.
// ---------------------------------------------------------------------------

export function excerpt(text: string): string {
  // Drop control characters, then flatten whitespace to single spaces.
  const flat = text.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
  return flat.length > MAX_EXCERPT_CHARS ? flat.slice(0, MAX_EXCERPT_CHARS) + "…" : flat;
}

export interface JournalRecord {
  at: string;
  network: "devnet";
  model: string;
  attempt: number;
  // Idempotency-Key sent with the request; the gateway files its receipt
  // under this id at GET /v1/keys/me/receipts, failed requests included.
  request_id: string;
  ok: boolean;
  reason: string | null;
  latency_ms: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  response_id: string | null;
  reply_excerpt: string | null;
  cost_flop: number | null;
  channel_id: string | null;
  settlement_status: string | null;
  miner: string | null;
}

export function journalRecord(
  config: ProbeConfig,
  attempt: number,
  requestId: string,
  startedAt: Date,
  latencyMs: number,
  result: ParsedReply | ParsedFailure,
): JournalRecord {
  const receipt = result.ok ? result.receipt : null;
  return {
    at: startedAt.toISOString(),
    network: config.network,
    model: config.model,
    attempt,
    request_id: requestId,
    ok: result.ok,
    reason: result.ok ? null : result.reason,
    latency_ms: Math.max(0, Math.round(latencyMs)),
    prompt_tokens: result.ok ? result.promptTokens : null,
    completion_tokens: result.ok ? result.completionTokens : null,
    response_id: result.ok ? result.responseId : null,
    reply_excerpt: result.ok ? excerpt(result.text) : null,
    cost_flop: receipt?.costFlop ?? null,
    channel_id: receipt?.channelId ?? null,
    settlement_status: receipt?.settlementStatus ?? null,
    miner: receipt?.miner ?? null,
  };
}
