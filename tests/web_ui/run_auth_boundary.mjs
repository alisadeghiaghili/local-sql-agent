// tests/web_ui/run_auth_boundary.mjs
//
// Node-side half of tests/web_ui/test_web_ui_auth_boundary.py.
//
// Phase 8 added `Authorization: Bearer <key>` auth to every backend route
// except GET /health, and web/js/api.js never learned to send it — every
// authenticated call from web/ 401'd. A Python test cannot exercise that
// bug directly (the request-building code lives in browser JS, not
// Python), so this script actually imports and runs the REAL
// web/js/api.js and web/js/apikey.js source (via a caller-supplied path
// to a copy with only the internal `./apikey.js` import rewritten to
// `./apikey.mjs` -- see the Python test for why the rename is necessary
// and why it is the only change made to the source) under a mocked
// `fetch`/`localStorage`, and asserts what request each Api method
// actually sent: does the Authorization header appear on every
// authenticated v2 call, and is it correctly absent from the
// unauthenticated GET /health call.
//
// Usage: node run_auth_boundary.mjs <path-to-copied-api.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed. Any assertion failure throws, which Node reports on stderr with
// a non-zero exit code -- the Python test treats anything other than a
// clean 0 + that marker as a failure.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const apiMjsPath = process.argv[2];
if (!apiMjsPath) {
  console.error("usage: node run_auth_boundary.mjs <path-to-copied-api.mjs>");
  process.exit(2);
}

// ---------------------------------------------------------------------
// Minimal in-memory localStorage -- Node has no Web Storage global by
// default. apikey.js (imported transitively by api.js) reads/writes this
// exactly the way a browser would.
// ---------------------------------------------------------------------
globalThis.localStorage = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
})();

// ---------------------------------------------------------------------
// Mocked fetch: records every call (url + headers actually sent) and
// returns whatever the current scenario queued up in `nextResponse`.
// ---------------------------------------------------------------------
const calls = [];
let nextResponse = null;

globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), headers: { ...((init && init.headers) || {}) } });
  if (typeof nextResponse === "function") return nextResponse();
  return nextResponse;
};

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    body: {
      // Minimal ReadableStream-like reader: no chunks, immediately done.
      // Only exercised by the streaming scenario below.
      getReader: () => ({ read: async () => ({ done: true, value: undefined }) }),
    },
  };
}

const { Api, UnauthorizedError, RateLimitError, ApiError } = await import(pathToFileURL(apiMjsPath).href);
const apikeyMjsPath = apiMjsPath.replace(/api\.mjs$/, "apikey.mjs");
const { setApiKey, clearApiKey, hasApiKey } = await import(pathToFileURL(apikeyMjsPath).href);

const api = new Api("http://backend.example");

function lastCall() {
  return calls[calls.length - 1];
}

// ---------------------------------------------------------------------
// Scenario 1: GET /health with NO key stored must not send an
// Authorization header at all -- this is the one route Phase 8 left
// open, and it must keep working for a caller with no key.
// ---------------------------------------------------------------------
clearApiKey();
assert.equal(hasApiKey(), false);
nextResponse = jsonResponse(200, { status: "ok", ollama: true, database: true });
await api.health();
assert.equal(lastCall().url, "http://backend.example/health");
assert.equal(
  "authorization" in Object.fromEntries(Object.entries(lastCall().headers).map(([k, v]) => [k.toLowerCase(), v])),
  false,
  "GET /health must not send an Authorization header when no key is stored",
);
console.log("[ok] /health omits Authorization when no key is stored");

// ---------------------------------------------------------------------
// Scenario 2: GET /health WITH a key stored sends it anyway (it unlocks
// the `model` field for an authenticated caller -- api/server.py) but
// must still succeed either way.
// ---------------------------------------------------------------------
setApiKey("analyst-key-abc");
nextResponse = jsonResponse(200, { status: "ok", ollama: true, database: true, model: "gpt-oss" });
const health = await api.health();
assert.equal(lastCall().headers["Authorization"], "Bearer analyst-key-abc");
assert.equal(health.model, "gpt-oss");
console.log("[ok] /health sends Authorization when a key is stored, without requiring one");

// ---------------------------------------------------------------------
// Scenario 3: an authenticated v2 call (createSession) with NO key
// stored must still be attempted with no Authorization header (this
// mirrors the real bug: the pre-fix code never attached one anywhere),
// and the resulting 401 must surface as UnauthorizedError, not a
// generic ApiError -- this is the exact distinction the UI's 401
// handling depends on to show "your key was rejected" instead of a
// generic error banner.
// ---------------------------------------------------------------------
clearApiKey();
nextResponse = jsonResponse(401, { error: { code: "UNAUTHENTICATED", message: "Missing or invalid API key." } });
let threw = null;
try {
  await api.createSession();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof UnauthorizedError, `expected UnauthorizedError, got ${threw && threw.constructor.name}`);
assert.equal(threw.message, "Missing or invalid API key.");
assert.equal(
  "Authorization" in lastCall().headers,
  false,
  "no key stored means no Authorization header should have been sent",
);
console.log("[ok] POST /v2/sessions with no key: no Authorization header sent, 401 -> UnauthorizedError");

// ---------------------------------------------------------------------
// Scenario 4: THE boundary that broke in production. With a key stored,
// every authenticated v2 call site must attach
// `Authorization: Bearer <key>` -- createSession, askTurn (JSON POST),
// askTurnStreaming (SSE POST), and patchAssumptions (PATCH). A test that
// only checked one of these would not have caught the original bug,
// since the fix has to live in the one shared `_fetchV2` chokepoint but
// nothing guarantees every call site actually routes through it.
// ---------------------------------------------------------------------
setApiKey("analyst-key-xyz");
const expectedAuth = "Bearer analyst-key-xyz";

nextResponse = jsonResponse(200, { session_id: "s1" });
await api.createSession();
assert.equal(lastCall().headers["Authorization"], expectedAuth, "createSession must send Authorization");

nextResponse = jsonResponse(200, { turn_id: "t1", session_id: "s1" });
await api.askTurn("s1", "how many auctions last week?");
assert.equal(lastCall().headers["Authorization"], expectedAuth, "askTurn must send Authorization");
assert.equal(lastCall().headers["Content-Type"], "application/json", "askTurn must still send Content-Type");

nextResponse = jsonResponse(200, {});
await api.askTurnStreaming("s1", "and refine that", () => {});
assert.equal(lastCall().headers["Authorization"], expectedAuth, "askTurnStreaming must send Authorization");

nextResponse = jsonResponse(200, { turn_id: "t1" });
await api.patchAssumptions("s1", "t1", [{ field: "date_range", value: "last_week" }]);
assert.equal(lastCall().headers["Authorization"], expectedAuth, "patchAssumptions must send Authorization");

console.log("[ok] every authenticated v2 call site (createSession/askTurn/askTurnStreaming/patchAssumptions) sends Authorization: Bearer <key>");

// ---------------------------------------------------------------------
// Scenario 5: 429 is a distinct, structured error -- not a generic
// ApiError -- carrying retry_after_seconds through so the UI can show a
// specific "try again in Ns" message instead of a generic failure.
// ---------------------------------------------------------------------
nextResponse = jsonResponse(429, {
  error: {
    code: "RATE_LIMIT_EXCEEDED",
    message: "Rate limit exceeded (this is a client throttling response, not a query or model failure). Retry after 12.3s.",
    retry_after_seconds: 12.3,
  },
});
threw = null;
try {
  await api.createSession();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof RateLimitError, `expected RateLimitError, got ${threw && threw.constructor.name}`);
assert.equal(threw.retryAfterSeconds, 12.3);
assert.ok(!(threw instanceof UnauthorizedError));
console.log("[ok] 429 -> RateLimitError carrying retry_after_seconds, distinct from UnauthorizedError/ApiError");

// ---------------------------------------------------------------------
// Scenario 6: a generic non-2xx (e.g. 500) is still a plain ApiError,
// and its message is read from the REAL error envelope shape
// (`{"error": {"message": ...}}`), not a nonexistent top-level `detail`
// field -- the pre-fix fallback read `j.detail`, which never matched
// api/errors.py's actual envelope, so every such error rendered as a
// bare "HTTP <status>" with the real message silently dropped.
// ---------------------------------------------------------------------
nextResponse = jsonResponse(500, { error: { code: "INTERNAL_ERROR", message: "boom" } });
threw = null;
try {
  await api.createSession();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof ApiError);
assert.ok(!(threw instanceof UnauthorizedError) && !(threw instanceof RateLimitError));
assert.equal(threw.message, "boom", "ApiError must read the message out of the real {error:{message}} envelope");
console.log("[ok] generic non-2xx -> ApiError with the real error.message (not a nonexistent top-level `detail`)");

console.log("ALL_SCENARIOS_PASSED");
