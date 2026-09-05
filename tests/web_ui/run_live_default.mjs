// tests/web_ui/run_live_default.mjs
//
// Node-side half of tests/web_ui/test_web_ui_live_default.py.
//
// Before this change, web/js/state.js's `state.mode` initialized to
// "simulated": a fresh load of a LIVE deployment showed synthetic, made-up
// numbers by default, in the exact same UI as real ones, until an analyst
// noticed and clicked the "زندهٔ API" toggle themselves. This drives the
// REAL web/js/state.js and web/js/config.js source (via caller-supplied
// paths to copies that are otherwise byte-identical to the real files --
// neither has an internal import specifier to rewrite, unlike the other
// harnesses in this directory) under a mocked localStorage, and asserts,
// at the actual boundary that changed:
//
// * a fresh import of state.js has `state.mode === "live"` -- the default
//   itself, not just "some function returns live for some input";
// * config.js exports a real, non-empty DEFAULT_BASE_URL -- the "one file
//   a deployment edits" this change adds;
// * resolveBootMode's precedence: `?live=1` -> "live", `?live=0` ->
//   "simulated" (this is new: before this change there was no supported
//   way to force simulated via the URL at all, since it was already the
//   default), anything else -> whatever default it's given, unchanged;
// * resolveBootBaseUrl's precedence: `?base=` always overrides; with no
//   `?base=`, a value already resolved from localStorage (via a full
//   loadPersisted() round-trip against a mocked localStorage, not just a
//   hand-built string) is honoured untouched -- this is the exact
//   "operator points a single browser somewhere else for debugging" path
//   the brief requires to keep working once the visible top-bar control
//   is gone.
//
// Usage: node run_live_default.mjs <path-to-copied-state.mjs> <path-to-copied-config.mjs>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario below
// passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [stateMjsPath, configMjsPath] = process.argv.slice(2);
if (!stateMjsPath || !configMjsPath) {
  console.error("usage: node run_live_default.mjs <path-to-copied-state.mjs> <path-to-copied-config.mjs>");
  process.exit(2);
}

// ---------------------------------------------------------------------
// Minimal in-memory localStorage -- Node has no Web Storage global by
// default (same shim as run_auth_boundary.mjs).
// ---------------------------------------------------------------------
const storageBacking = new Map();
globalThis.localStorage = {
  getItem: (k) => (storageBacking.has(k) ? storageBacking.get(k) : null),
  setItem: (k, v) => storageBacking.set(k, String(v)),
  removeItem: (k) => storageBacking.delete(k),
  clear: () => storageBacking.clear(),
};

// state.js's applyTheme() touches document.documentElement -- not
// exercised by any scenario below (none call applyTheme), so a minimal
// stand-in is enough to let the module import cleanly regardless.
globalThis.document = { documentElement: { removeAttribute() {}, setAttribute() {} } };

const { state, loadPersisted, resolveBootMode, resolveBootBaseUrl } =
  await import(pathToFileURL(stateMjsPath).href);
const { DEFAULT_BASE_URL } = await import(pathToFileURL(configMjsPath).href);

/* ── Scenario 1: THE default itself. ────────────────────────────────── */
assert.equal(state.mode, "live", "state.mode must default to \"live\", not \"simulated\"");
console.log("[ok] state.mode defaults to \"live\"");

/* ── Scenario 2: config.js is real and usable. ──────────────────────── */
assert.equal(typeof DEFAULT_BASE_URL, "string");
assert.ok(DEFAULT_BASE_URL.length > 0, "DEFAULT_BASE_URL must not be an empty string");
console.log(`[ok] web/js/config.js exports a non-empty DEFAULT_BASE_URL (${DEFAULT_BASE_URL})`);

/* ── Scenario 3: resolveBootMode's precedence. `?live=0` forcing
 * simulated is NEW -- before this change nothing needed to force
 * simulated via the URL, since simulated was already the default. ───── */
assert.equal(resolveBootMode(new URLSearchParams(""), "live"), "live", "no ?live= param: default (live) stands");
assert.equal(resolveBootMode(new URLSearchParams("?live=1"), "live"), "live", "?live=1 -> live");
assert.equal(resolveBootMode(new URLSearchParams("?live=1"), "simulated"), "live", "?live=1 -> live even if default were simulated");
assert.equal(resolveBootMode(new URLSearchParams("?live=0"), "live"), "simulated", "?live=0 -> simulated, overriding the live default");
assert.equal(resolveBootMode(new URLSearchParams("?live=0"), "simulated"), "simulated", "?live=0 -> simulated");
assert.equal(resolveBootMode(new URLSearchParams("?live=banana"), "live"), "live", "an unrecognized ?live= value leaves the default untouched");
console.log("[ok] resolveBootMode: ?live=1 -> live, ?live=0 -> simulated, anything else -> the given default");

/* ── Scenario 4: resolveBootBaseUrl's precedence, combined with a REAL
 * loadPersisted() round-trip against the mocked localStorage -- not just
 * a hand-built string -- to prove the persisted value survives all the
 * way from storage into the resolved boot base URL when no `?base=` is
 * given, and that `?base=` still overrides it when one is. ──────────── */
storageBacking.set("lsa-web-base", "http://from-storage:7000");
state.baseUrl = DEFAULT_BASE_URL; // main.js's boot order: deploy default first...
loadPersisted(); // ...then a persisted override, if any, on top of it
assert.equal(state.baseUrl, "http://from-storage:7000", "loadPersisted() must honour a value already saved to localStorage");

const noBaseParam = new URLSearchParams("");
assert.equal(
  resolveBootBaseUrl(noBaseParam, state.baseUrl), "http://from-storage:7000",
  "with no ?base= param, the persisted localStorage value must still be honoured, not silently reset to the deploy default",
);

const withBaseParam = new URLSearchParams("?base=http://override-for-debugging:1234");
assert.equal(
  resolveBootBaseUrl(withBaseParam, state.baseUrl), "http://override-for-debugging:1234",
  "?base= must still override even a persisted localStorage value",
);
console.log("[ok] resolveBootBaseUrl: persisted localStorage value honoured when no ?base=; ?base= still overrides it when present");

/* ── Scenario 5: with NEITHER a persisted value nor a `?base=` param,
 * config.js's deploy-time default is what's left standing. ──────────── */
storageBacking.clear();
state.baseUrl = DEFAULT_BASE_URL;
loadPersisted();
assert.equal(state.baseUrl, DEFAULT_BASE_URL, "with nothing persisted, the deploy-time default from config.js stands");
assert.equal(resolveBootBaseUrl(new URLSearchParams(""), state.baseUrl), DEFAULT_BASE_URL);
console.log("[ok] with no persisted value and no ?base=, config.js's DEFAULT_BASE_URL is the effective base URL");

console.log("ALL_SCENARIOS_PASSED");
