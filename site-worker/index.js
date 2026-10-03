/*
 * Cloudflare Worker entrypoint for the private AEGIS Site.
 *
 * The build step in scripts/build-site-worker.mjs embeds the existing static
 * dashboard assets. The Worker adds the Site-hosted MCP bridge without
 * moving broker credentials or execution authority into the Site.
 */

const HTML = __AEGIS_HTML__;
const APP_JS = __AEGIS_APP_JS__;
const STYLES_CSS = __AEGIS_STYLES_CSS__;

const RELAY_VERSION = "aegis-news-relay-v1";
const RELAY_TTL_MS = 10 * 60 * 1000;
const MAX_CONTEXTS = 96;
const MAX_BODY_BYTES = 262_144;

function json(value, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}

function text(value, contentType) {
  return new Response(value, {
    headers: {
      "content-type": contentType,
      "cache-control": "no-store",
    },
  });
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function hexBytes(raw) {
  if (typeof raw !== "string" || !/^[0-9a-f]{64}$/i.test(raw.trim())) return null;
  const value = raw.trim();
  const bytes = new Uint8Array(32);
  for (let index = 0; index < 32; index += 1) bytes[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  return bytes;
}

function hexSignature(bytes) {
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function finiteInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.trunc(number) : fallback;
}

function safeScalar(value, max = 240) {
  if (typeof value === "string") return value.slice(0, max);
  if (typeof value === "number" || typeof value === "boolean") return value;
  return undefined;
}

const CONTEXT_FIELDS = new Set([
  "asset_class", "freshness", "aggregate_sentiment", "aggregate_relevance",
  "aggregate_confidence", "aggregate_source_reliability", "aggregate_impact",
  "event_risk", "conflicting_news", "unique_event_count", "material_event_count",
  "latest_material_event_timestamp", "event_summaries", "news_risk_flags",
  "explanation", "as_of",
]);

const GLOBAL_FIELDS = new Set([
  "as_of", "macro_risk", "geopolitical_risk", "monetary_policy_risk",
  "market_stress", "major_events", "high_impact_event_count", "freshness",
  "explanation",
]);

function sanitizeContext(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const result = {};
  for (const key of CONTEXT_FIELDS) {
    const item = value[key];
    if (item === undefined || item === null) continue;
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") result[key] = safeScalar(item);
    else if (Array.isArray(item)) result[key] = item.slice(0, 8).map((entry) => String(entry).slice(0, 240));
  }
  if (!["NEWS_FRESH", "NEWS_DELAYED"].includes(result.freshness)) return null;
  if (typeof result.latest_material_event_timestamp !== "string") return null;
  return result;
}

function sanitizeGlobal(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const result = {};
  for (const key of GLOBAL_FIELDS) {
    const item = value[key];
    if (item === undefined || item === null) continue;
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") result[key] = safeScalar(item);
    else if (Array.isArray(item)) result[key] = item.slice(0, 8).map((entry) => String(entry).slice(0, 240));
  }
  return Object.keys(result).length ? result : undefined;
}

function normalizeContexts(value) {
  const entries = Array.isArray(value)
    ? value.map((item) => [item?.symbol || item?.ticker, item])
    : Object.entries(value || {});
  const result = {};
  for (const [rawSymbol, rawContext] of entries) {
    const symbol = String(rawSymbol || "").trim().slice(0, 64);
    if (!symbol || Object.keys(result).length >= MAX_CONTEXTS) continue;
    const context = sanitizeContext(rawContext);
    if (context) result[symbol] = context;
  }
  return result;
}

async function signedRelay(args, env) {
  const key = hexBytes(env.AEGIS_SECONDARY_NEWS_RELAY_KEY);
  const relayUrl = String(env.AEGIS_SECONDARY_NEWS_RELAY_URL || "https://relay.aquariusageai.com/api/secondary/news").trim();
  if (!key) throw new Error("RELAY_KEY_NOT_CONFIGURED");
  const parsed = new URL(relayUrl);
  if (parsed.protocol !== "https:") throw new Error("RELAY_URL_MUST_USE_HTTPS");

  const now = new Date();
  const providedAsOf = args.as_of ? new Date(String(args.as_of)) : now;
  const asOf = Number.isNaN(providedAsOf.getTime()) ? now : providedAsOf;
  const body = {
    version: RELAY_VERSION,
    source_id: "site-market-research",
    generated_at: now.toISOString(),
    as_of: asOf.toISOString(),
    expires_at: new Date(now.getTime() + RELAY_TTL_MS).toISOString(),
    provider: String(args.provider || "scheduled-market-research").slice(0, 80),
    provider_status: String(args.provider_status || "PARTIAL").slice(0, 40),
    events_received: finiteInteger(args.events_received),
    events_fresh: finiteInteger(args.events_fresh),
    events_material: finiteInteger(args.events_material),
    contexts: normalizeContexts(args.contexts),
  };
  const global = sanitizeGlobal(args.global_risk_context);
  if (global) body.global_risk_context = global;
  const envelope = {
    body,
    signature: hexSignature(await crypto.subtle.sign(
      "HMAC",
      await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]),
      new TextEncoder().encode(canonical(body)),
    )),
  };
  const serialized = JSON.stringify(envelope);
  if (new TextEncoder().encode(serialized).byteLength > MAX_BODY_BYTES) throw new Error("RELAY_PAYLOAD_TOO_LARGE");
  const response = await fetch(parsed.toString(), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "user-agent": "AEGIS-Site-Market-Research/1",
      "x-aegis-news-relay-signature": envelope.signature,
    },
    body: serialized,
  });
  if (!response.ok) throw new Error(`RELAY_HTTP_${response.status}`);
  return { status: "SENT", shadow_only: true, contexts: Object.keys(body.contexts).length, source_id: body.source_id };
}

const MCP_TOOL = {
  name: "submit_news_evidence",
  description: "Invia al runner AEGIS solo evidenza news sanitizzata e firmata. Non apre ordini e non modifica il broker.",
  inputSchema: {
    type: "object",
    additionalProperties: false,
    required: ["contexts"],
    properties: {
      provider: { type: "string" },
      provider_status: { type: "string" },
      as_of: { type: "string", description: "Timestamp ISO-8601 dell'evidenza." },
      events_received: { type: "integer", minimum: 0 },
      events_fresh: { type: "integer", minimum: 0 },
      events_material: { type: "integer", minimum: 0 },
      contexts: { type: "object", description: "Mappa simbolo -> contesto news con freshness NEWS_FRESH o NEWS_DELAYED e latest_material_event_timestamp." },
      global_risk_context: { type: "object" },
    },
  },
};

function mcpResult(id, result) {
  return json({ jsonrpc: "2.0", id, result });
}

async function handleMcp(request, env) {
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405, { allow: "POST" });
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) return json({ error: "request_too_large" }, 413);
  let payload;
  try { payload = await request.json(); } catch { return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Invalid JSON" } }, 400); }
  const { id = null, method, params = {} } = payload || {};
  if (method === "notifications/initialized") return new Response(null, { status: 202 });
  if (method === "initialize") {
    return mcpResult(id, {
      protocolVersion: String(params.protocolVersion || "2025-06-18"),
      capabilities: { tools: {} },
      serverInfo: { name: "aegis-invest-ai", version: "1.0.0" },
    });
  }
  if (method === "tools/list") return mcpResult(id, { tools: [MCP_TOOL] });
  if (method !== "tools/call") return json({ jsonrpc: "2.0", id, error: { code: -32601, message: "Method not found" } }, 404);
  if (!request.headers.get("oai-authenticated-user-id") && !request.headers.get("oai-authenticated-user-email")) {
    return mcpResult(id, { isError: true, content: [{ type: "text", text: "Owner authentication required" }] });
  }
  if (params.name !== MCP_TOOL.name) return mcpResult(id, { isError: true, content: [{ type: "text", text: "Unknown tool" }] });
  try {
    const result = await signedRelay(params.arguments || {}, env);
    return mcpResult(id, { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result });
  } catch (error) {
    return mcpResult(id, { isError: true, content: [{ type: "text", text: String(error?.message || error) }] });
  }
}

async function proxyApi(request, env, url) {
  const origin = String(env.AEGIS_API_ORIGIN || "").replace(/\/$/, "");
  const token = String(env.AEGIS_DASHBOARD_ACCESS_TOKEN || "");
  if (!origin || token.length < 32) return json({ status: "AEGIS_API_NOT_CONFIGURED" }, 503);
  const upstream = new URL(`${origin}${url.pathname}${url.search}`);
  const headers = new Headers(request.headers);
  headers.set("authorization", `Bearer ${token}`);
  headers.delete("host");
  headers.delete("cookie");
  return fetch(new Request(upstream, { method: request.method, headers, body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body }), { redirect: "manual" });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/mcp") return handleMcp(request, env);
    if (url.pathname.startsWith("/api/")) return proxyApi(request, env, url);
    if (url.pathname === "/" || url.pathname === "/index.html") return text(HTML, "text/html; charset=utf-8");
    if (url.pathname === "/app.js") return text(APP_JS, "text/javascript; charset=utf-8");
    if (url.pathname === "/styles.css") return text(STYLES_CSS, "text/css; charset=utf-8");
    return new Response("Not found", { status: 404 });
  },
};
