// tests/web_ui/run_prism_flag.mjs
//
// Node-side half of tests/web_ui/test_web_ui_prism_flag.py.
//
// Loads the REAL web/js/prism-tsql-patch.js and checks the one thing the
// existing stub-based test (run_sql_highlight.mjs) never did: that the
// idempotency marker the patch writes onto the SQL grammar is NOT an
// enumerable own property.
//
// Why it matters
// --------------
// Prism tokenises by iterating the grammar object's keys and treating each
// value as a token (a RegExp, or an object with a `.pattern`). The patch
// marks "already patched" with `__tsqlPatched` ON THE GRAMMAR OBJECT. If that
// marker is enumerable, Prism's own iteration reaches it, tries to use the
// boolean `true` as a token, and throws `true.exec is not a function`.
// `highlightSql` catches that and falls back to plain text -- so SQL stopped
// being highlighted at all, silently, with only the fallback showing. The
// existing test asserted the marker is idempotent, never that it is hidden
// from enumeration, so it stayed green while highlighting was dead in the
// browser.
//
// The patch is a classic script (an IIFE that assigns
// window.patchPrismForTsql and calls it once). It is loaded here by evaluating
// its source with a `window` shim in scope, exactly the realm shape it expects
// -- not imported as a module.
//
// Usage: node run_prism_flag.mjs <path-to-prism-tsql-patch.js>

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const patchPath = process.argv[2];
if (!patchPath) {
  console.error("usage: node run_prism_flag.mjs <prism-tsql-patch.js path>");
  process.exit(2);
}

// The realm the classic script expects. Its trailing self-invocation runs
// patchPrismForTsql(window.Prism) with Prism undefined; the patch guards
// `if (!lib || !lib.languages || !lib.languages.sql) return`, so that no-ops.
const windowShim = {};
const src = readFileSync(patchPath, "utf8");
const load = new Function("window", src);
load(windowShim);

assert.equal(
  typeof windowShim.patchPrismForTsql,
  "function",
  "prism-tsql-patch.js must expose window.patchPrismForTsql",
);

// A grammar shaped like Prism's real SQL grammar: keys whose values are the
// tokens Prism will iterate. If the patch adds an ENUMERABLE key, it lands in
// this same iteration and Prism breaks on it.
// Shaped like Prism's real SQL grammar (web/assets/vendor/prism-sql.min.js):
// it defines `string` and `identifier`, which the patch prepends its own
// rules onto -- so the fixture must carry both, or the patch's
// `.concat([sql.identifier])` fallback appends `[undefined]`, an edge that
// cannot occur against the real grammar.
const grammar = {
  comment: /(^|[^\\])(?:\/\*[\s\S]*?\*\/|(?:--).*)/,
  string: { pattern: /'[^']*'/, greedy: true },
  identifier: { pattern: /`[^`]*`/, greedy: true },
  keyword: /\b(?:SELECT|FROM|WHERE)\b/i,
};
const lib = { languages: { sql: grammar } };

windowShim.patchPrismForTsql(lib);
const sql = lib.languages.sql;

// 1. The marker is set and readable (idempotency still works).
assert.equal(sql.__tsqlPatched, true, "the patch did not set its __tsqlPatched marker");
assert.ok("__tsqlPatched" in sql, "the marker must remain readable for the idempotency guard");

// 2. THE FIX: the marker must not be enumerable -- Prism iterates enumerable
//    keys, and a boolean among them is what threw `true.exec is not a function`.
const enumerableKeys = Object.keys(sql);
assert.ok(
  !enumerableKeys.includes("__tsqlPatched"),
  "__tsqlPatched is an ENUMERABLE key on the SQL grammar: " +
    JSON.stringify(enumerableKeys) +
    ". Prism iterates these as tokens and throws on the boolean, killing all " +
    "SQL highlighting. Set it with Object.defineProperty(..., {enumerable:false}).",
);

// 3. Every remaining enumerable value is still a usable token (a RegExp, or an
//    object carrying a `.pattern`) -- i.e. nothing non-tokenlike leaked in.
for (const key of enumerableKeys) {
  const value = sql[key];
  const values = Array.isArray(value) ? value : [value];
  for (const v of values) {
    const tokenLike = v instanceof RegExp || (v && typeof v === "object" && "pattern" in v);
    assert.ok(
      tokenLike,
      `grammar key ${JSON.stringify(key)} holds a non-token value Prism would choke on: ${String(v)}`,
    );
  }
}

// 4. The patch still did its job: T-SQL bracket identifiers and N'' strings.
assert.ok(sql.identifier, "the patch no longer adds a bracket-identifier rule");

// 5. Idempotent: a second call must not double-apply or re-introduce the marker
//    enumerably.
windowShim.patchPrismForTsql(lib);
assert.ok(
  !Object.keys(lib.languages.sql).includes("__tsqlPatched"),
  "a second patch call re-introduced an enumerable __tsqlPatched",
);

console.log("ALL_SCENARIOS_PASSED");
