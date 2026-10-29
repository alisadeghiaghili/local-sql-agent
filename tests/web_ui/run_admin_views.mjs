// tests/web_ui/run_admin_views.mjs
//
// Node-side half of tests/web_ui/test_web_ui_admin_views.py.
//
// Drives the REAL web/admin/views.js against a small fake of the admin
// page (its real section ids, two-column wrappers and jump links, read
// from web/admin/index.html by the Python wrapper) to prove:
//
// * each named view shows exactly its sections and hides the rest, and a
//   two-column wrapper is hidden only when both of its sections are;
// * «همه» shows every section (the page as it was), and so does an
//   unknown view id, so a bad stored value can never empty the page;
// * the jump links follow the view; the summary rail's link never hides;
// * a #sec-* hash opens the view that holds it, before a remembered view;
// * the view buttons carry aria-pressed and the attention marks carry
//   screen-reader text, not colour alone;
// * storage that throws is ignored.
//
// Usage: node run_admin_views.mjs <copied-views.mjs> <page.json>
//
// Exits 0 and prints "ALL_SCENARIOS_PASSED" iff every scenario passed.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [viewsPath, pagePath] = process.argv.slice(2);
if (!viewsPath || !pagePath) {
  console.error("usage: node run_admin_views.mjs <views.mjs> <page.json>");
  process.exit(2);
}
const page = JSON.parse(readFileSync(pagePath, "utf-8"));

/* ── Minimal DOM fake: tag, id, classes, attributes, children. ────────── */
class El {
  constructor(tag, { id = "", classes = [], attrs = {} } = {}) {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this._classes = new Set(classes);
    this._attrs = new Map(Object.entries(attrs));
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.dataset = {};
    this._listeners = {};
    this.textContent = "";
  }
  get firstChild() { return this.children[0] || null; }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() {
    const s = this._classes;
    return {
      add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !s.has(c) : Boolean(force);
        if (on) s.add(c); else s.delete(c);
        return on;
      },
    };
  }
  setAttribute(k, v) { this._attrs.set(k, String(v)); }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
  insertBefore(n, ref) {
    n.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    return n;
  }
  addEventListener(t, h) { (this._listeners[t] ||= []).push(h); }
  click() { for (const h of this._listeners.click || []) h({}); }
  _all() { const out = []; const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    let found = this._all().filter((e) => matches(e, parts[0]));
    for (const p of parts.slice(1)) found = found.flatMap((e) => e._all().filter((d) => matches(d, p)));
    return [...new Set(found)];
  }
}
function matches(el, simple) {
  const m = /^([a-z]+)?((?:\.[\w-]+)*)(?:\[(\w+)\^='([^']*)'\])?$/.exec(simple);
  if (!m) throw new Error(`unsupported selector ${simple}`);
  const [, tag, cls, attr, prefix] = m;
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  for (const c of (cls || "").split(".").filter(Boolean)) if (!el.classList.contains(c)) return false;
  if (attr) { const v = el.getAttribute(attr); if (v === null || !v.startsWith(prefix)) return false; }
  return true;
}
globalThis.document = { createElement: (t) => new El(t) };

/* Build the page from the real ids: a nav of jump links, then the sections
 * in document order, the paired ones inside their .admin-columns. */
function buildPage() {
  const root = new El("body");
  const nav = root.appendChild(new El("nav", { id: "admin-jump", classes: ["admin-jump"] }));
  for (const href of page.links) nav.appendChild(new El("a", { attrs: { href } }));
  const main = root.appendChild(new El("main"));
  main.appendChild(new El("section", { id: "overview", classes: ["admin-summary-rail"] }));
  for (const group of page.layout) {
    const host = group.length > 1 ? main.appendChild(new El("div", { classes: ["admin-columns"] })) : main;
    for (const id of group) host.appendChild(new El("section", { id, classes: ["admin-section"] }));
  }
  root.nav = nav;
  return root;
}
const sections = (root) => root.querySelectorAll("section.admin-section");
const shownIds = (root) => sections(root).filter((s) => !s.hidden).map((s) => s.id);
const linkFor = (root, id) => root.querySelectorAll(".admin-jump a[href^='#']").find((a) => a.getAttribute("href") === `#${id}`);

const views = await import(pathToFileURL(viewsPath).href);
const { VIEWS, applyView, initialView, markAttention, renderViewSwitch, viewForSection, readRememberedView, rememberView, DEFAULT_VIEW } = views;
const allIds = page.layout.flat();

// ---------------------------------------------------------------------
// Scenario 1: each named view shows exactly its own sections.
// ---------------------------------------------------------------------
{
  for (const v of VIEWS.filter((x) => x.sections)) {
    const root = buildPage();
    assert.equal(applyView(root, v.id), v.id);
    assert.deepEqual(new Set(shownIds(root)), new Set(v.sections), `view ${v.id}`);
    for (const wrap of root.querySelectorAll(".admin-columns")) {
      const kids = wrap.querySelectorAll("section.admin-section");
      assert.equal(wrap.hidden, kids.every((k) => k.hidden), `wrapper of ${kids.map((k) => k.id)} in view ${v.id}`);
    }
    for (const id of allIds) {
      assert.equal(linkFor(root, id).hidden, !v.sections.includes(id), `jump link #${id} in view ${v.id}`);
    }
    assert.equal(linkFor(root, "overview").hidden, false, "the summary rail's link is never hidden");
  }
  console.log("[ok] each named view shows exactly its sections, wrappers and links");
}

// ---------------------------------------------------------------------
// Scenario 2: «همه» and an unknown id show the page as it was.
// ---------------------------------------------------------------------
{
  for (const id of ["all", "no-such-view", undefined]) {
    const root = buildPage();
    applyView(root, "status");
    assert.equal(applyView(root, id), "all");
    assert.deepEqual(shownIds(root), allIds, `every section for ${id}`);
    assert.ok(root.querySelectorAll(".admin-columns").every((w) => !w.hidden));
    assert.ok(root.querySelectorAll(".admin-jump a[href^='#']").every((a) => !a.hidden));
  }
  console.log("[ok] «همه», and any unknown view id, show every section");
}

// ---------------------------------------------------------------------
// Scenario 3: the opening view.
// ---------------------------------------------------------------------
{
  assert.equal(initialView("", null), DEFAULT_VIEW);
  assert.equal(DEFAULT_VIEW, "status");
  assert.equal(initialView("", "access"), "access", "the remembered view");
  assert.equal(initialView("", "garbage"), DEFAULT_VIEW, "a bad remembered value is ignored");
  assert.equal(initialView("#sec-keys", "quality"), "access", "a section hash wins over the memory");
  assert.equal(initialView("sec-quality", null), "quality");
  assert.equal(initialView("#overview", "data"), "data", "the rail is in every view");
  for (const id of allIds) assert.ok(viewForSection(id), `${id} belongs to a view`);
  console.log("[ok] hash, then memory, then the default");
}

// ---------------------------------------------------------------------
// Scenario 4: the buttons, aria-pressed, and attention marks.
// ---------------------------------------------------------------------
{
  const root = buildPage();
  const picked = [];
  const group = renderViewSwitch(root.nav, (v) => picked.push(v));
  assert.equal(root.nav.firstChild, group, "the buttons open the nav");
  const buttons = root.querySelectorAll(".admin-view-btn");
  assert.deepEqual(buttons.map((b) => b.dataset.view), VIEWS.map((v) => v.id));
  assert.ok(buttons.every((b) => b.type === "button"));
  buttons[2].click();
  assert.deepEqual(picked, [VIEWS[2].id]);
  applyView(root, "quality");
  for (const b of buttons) assert.equal(b.getAttribute("aria-pressed"), String(b.dataset.view === "quality"));

  markAttention(root, { status: true, quality: false });
  const status = buttons.find((b) => b.dataset.view === "status");
  const quality = buttons.find((b) => b.dataset.view === "quality");
  assert.ok(status.classList.contains("attn"));
  assert.equal(status.querySelector(".admin-view-attn").textContent, " (نیاز به توجه)", "said in words, not colour alone");
  assert.equal(quality.classList.contains("attn"), false);
  markAttention(root, {});
  assert.equal(status.classList.contains("attn"), false);
  assert.equal(status.querySelector(".admin-view-attn").textContent, "");
  console.log("[ok] buttons press, report aria-pressed, and mark attention in words");
}

// ---------------------------------------------------------------------
// Scenario 5: storage that throws is ignored.
// ---------------------------------------------------------------------
{
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(readRememberedView(broken), null);
  rememberView(broken, "access");
  assert.equal(readRememberedView(null), null);
  const mem = new Map();
  const ok = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) };
  rememberView(ok, "data");
  assert.equal(readRememberedView(ok), "data");
  console.log("[ok] storage failures are ignored");
}

console.log("ALL_SCENARIOS_PASSED");
