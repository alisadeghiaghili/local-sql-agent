// tests/web_ui/run_admin_time.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_readability.py.
// Drives the REAL web/admin/time.js: ISO strings from the server become a
// Persian calendar date and time, the original string is kept for the
// title, and anything that is not a date comes back unchanged.
//
// Usage: node run_admin_time.mjs <copied-time.mjs>
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [timePath] = process.argv.slice(2);
const { formatWhen, parseIso } = await import(pathToFileURL(timePath).href);
const UTC = { timeZone: "UTC" };

// Python's isoformat() with microseconds and no offset (the audit log).
{
  const r = formatWhen("2026-10-27T10:46:31.435668", UTC);
  assert.equal(r.title, "2026-10-27T10:46:31.435668");
  assert.ok(r.text.includes("آبان") && r.text.includes("۱۴۰۵"), r.text);
  assert.ok(/[۰-۹]{2}:[۰-۹]{2}/.test(r.text), `a time with Persian digits: ${r.text}`);
  assert.ok(!r.text.includes("T"), "no raw ISO left");
  console.log("[ok] microseconds, no offset ->", r.text);
}
// With Z and with an offset.
{
  assert.equal(formatWhen("2026-09-22T00:00:00Z", UTC).text.includes("۳۱ شهریور ۱۴۰۵"), true);
  const off = formatWhen("2026-10-29T22:51:20+03:30", UTC);
  assert.ok(off.text.includes("۷ آبان ۱۴۰۵"), off.text);
  console.log("[ok] Z and +03:30 offsets");
}
// A bare date stays the same calendar day.
{
  const r = formatWhen("2026-10-13");
  assert.ok(r.text.includes("۲۱ مهر ۱۴۰۵"), r.text);
  assert.ok(!r.text.includes("،"), "a bare date has no time part");
  console.log("[ok] a bare date ->", r.text);
}
// Not a date: unchanged, no title.
{
  for (const v of ["t", "—", "", "never", "2026-13-45", "12345"]) {
    const r = formatWhen(v, UTC);
    assert.equal(r.title, "", `no title for ${v}`);
    if (v !== "2026-13-45") assert.equal(r.text, v, `unchanged: ${v}`);
  }
  assert.deepEqual(formatWhen(null), { text: "", title: "" });
  assert.deepEqual(formatWhen(undefined), { text: "", title: "" });
  assert.equal(parseIso(42), null);
  console.log("[ok] values that are not dates come back unchanged");
}

console.log("ALL_SCENARIOS_PASSED");
