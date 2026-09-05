// tests/web_ui/run_admin_auth_boundary.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_auth_boundary.py.
//
// Mirrors run_auth_boundary.mjs's own technique exactly, for the admin
// panel (web/admin/admin.js) instead of the analyst UI (web/js/api.js):
// import and run the REAL web/admin/admin.js and web/js/apikey.js source
// (via a caller-supplied path to a copy with only the internal
// `../js/apikey.js` import rewritten to `./apikey.mjs`) under a mocked
// `fetch`/`localStorage`, and assert what request each AdminApi method
// actually sent -- does `Authorization: Bearer <key>` appear on every
// admin call, and does a 401/403 come back as the right, distinguishable
// typed error.
//
// Usage: node run_admin_auth_boundary.mjs <path-to-copied-admin.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed. Any assertion failure throws, which Node reports on stderr with
// a non-zero exit code.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const adminMjsPath = process.argv[2];
if (!adminMjsPath) {
  console.error("usage: node run_admin_auth_boundary.mjs <path-to-copied-admin.mjs>");
  process.exit(2);
}

// ---------------------------------------------------------------------
// Minimal in-memory localStorage -- same shape as run_auth_boundary.mjs.
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
  return nextResponse;
};

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const { AdminApi, AdminUnauthorizedError, AdminForbiddenError, AdminApiError } =
  await import(pathToFileURL(adminMjsPath).href);
const apikeyMjsPath = adminMjsPath.replace(/admin\.mjs$/, "apikey.mjs");
const { setApiKey, clearApiKey, hasApiKey } = await import(pathToFileURL(apikeyMjsPath).href);

const api = new AdminApi("http://backend.example");

function lastCall() {
  return calls[calls.length - 1];
}

// ---------------------------------------------------------------------
// Scenario 1: no key stored -> the call is still attempted with no
// Authorization header (the server's real 401 is what tells the operator
// a key is needed, not a client-side guess), and the 401 is promoted to
// AdminUnauthorizedError.
// ---------------------------------------------------------------------
clearApiKey();
assert.equal(hasApiKey(), false);
nextResponse = jsonResponse(401, { error: { code: "UNAUTHENTICATED", message: "Missing or invalid API key." } });
let threw = null;
try {
  await api.cache();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof AdminUnauthorizedError, `expected AdminUnauthorizedError, got ${threw && threw.constructor.name}`);
assert.equal(
  "Authorization" in lastCall().headers,
  false,
  "no key stored means no Authorization header should have been sent",
);
console.log("[ok] no key stored: no Authorization header sent, 401 -> AdminUnauthorizedError");

// ---------------------------------------------------------------------
// Scenario 2: a real, non-admin key stored -> the server's 403 comes back
// as AdminForbiddenError, distinguishable from AdminUnauthorizedError --
// this is the exact distinction the phase-1 spec's own UI requirement
// depends on ("say plainly that this key is not an admin key rather than
// showing an empty dashboard").
// ---------------------------------------------------------------------
setApiKey("analyst-key-not-admin");
nextResponse = jsonResponse(403, { error: { code: "ADMIN_REQUIRED", message: "This API key does not have admin access." } });
threw = null;
try {
  await api.summary();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof AdminForbiddenError, `expected AdminForbiddenError, got ${threw && threw.constructor.name}`);
assert.equal(threw.message, "This API key does not have admin access.");
assert.ok(!(threw instanceof AdminUnauthorizedError));
assert.equal(lastCall().headers["Authorization"], "Bearer analyst-key-not-admin");
console.log("[ok] non-admin key: 403 -> AdminForbiddenError, distinct from AdminUnauthorizedError");

// ---------------------------------------------------------------------
// Scenario 3: THE boundary this whole harness exists to prove. With an
// admin key stored, every one of the four admin call sites --
// summary, healthChecks, cache, config -- must attach
// `Authorization: Bearer <key>`. A test that only checked one would not
// catch a future call site added without going through `_get`.
// ---------------------------------------------------------------------
setApiKey("admin-key-xyz");
const expectedAuth = "Bearer admin-key-xyz";

nextResponse = jsonResponse(200, { mode: "aggregate_safe", record_count: 0 });
await api.summary();
assert.equal(lastCall().headers["Authorization"], expectedAuth, "summary() must send Authorization");
assert.ok(!lastCall().url.includes("include_examples"), "summary() with no args must not opt into include_examples");

nextResponse = jsonResponse(200, { mode: "aggregate_with_examples", record_count: 0 });
await api.summary(true);
assert.equal(lastCall().headers["Authorization"], expectedAuth, "summary(true) must send Authorization");
assert.ok(lastCall().url.includes("include_examples=true"), "summary(true) must opt into include_examples");

nextResponse = jsonResponse(200, { checks: [] });
await api.healthChecks();
assert.equal(lastCall().headers["Authorization"], expectedAuth, "healthChecks() must send Authorization");

nextResponse = jsonResponse(200, { hits: 0, misses: 0, evictions: 0, size: 0, enabled: true });
await api.cache();
assert.equal(lastCall().headers["Authorization"], expectedAuth, "cache() must send Authorization");

nextResponse = jsonResponse(200, { project_config_dir: "project_config", files: [] });
await api.config();
assert.equal(lastCall().headers["Authorization"], expectedAuth, "config() must send Authorization");

console.log("[ok] every admin call site (summary/healthChecks/cache/config) sends Authorization: Bearer <key>");

// ---------------------------------------------------------------------
// Scenario 4: a generic non-2xx (e.g. 500) is still a plain AdminApiError,
// reading the real error envelope shape, not a nonexistent `detail` field.
// ---------------------------------------------------------------------
nextResponse = jsonResponse(500, { error: { code: "INTERNAL_ERROR", message: "boom" } });
threw = null;
try {
  await api.cache();
} catch (err) {
  threw = err;
}
assert.ok(threw instanceof AdminApiError);
assert.ok(!(threw instanceof AdminUnauthorizedError) && !(threw instanceof AdminForbiddenError));
assert.equal(threw.message, "boom");
console.log("[ok] generic non-2xx -> AdminApiError with the real error.message");

console.log("ALL_SCENARIOS_PASSED");
