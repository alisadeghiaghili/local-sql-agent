/* web/admin/views.js — the admin panel's grouped views.
 *
 * The panel had grown to thirteen sections and a summary rail on one page, about 6,800 px
 * tall on a laptop screen: an operator looking for the keys card scrolled
 * past the audit tables, the quality tables twice and the vocabulary
 * table first. The sections now belong to four views, and the sticky nav
 * starts with one button per view. Only the chosen view's sections are
 * shown; the summary rail above them is always shown. «همه» shows every
 * section, which is the page as it was.
 *
 * Nothing is removed or moved in the DOM: a section outside the view gets
 * the `hidden` attribute, a two-column wrapper whose sections are all
 * hidden is hidden too, and the jump links of hidden sections are hidden
 * with them. A link to any `#sec-*` (a bookmark, the old jump nav, a
 * summary-rail item) still works: it first switches to the view that holds
 * the section. Every card keeps loading and refreshing as before, hidden
 * or not, so switching views never shows a stale or empty card.
 *
 * A pure DOM module with no fetch or state of its own, like
 * access-requests.js, so it is testable under Node without main.js's
 * bootstrap. The chosen view is remembered in localStorage (a per-browser
 * convenience; every read and write is guarded).
 */

"use strict";

/** The views, in nav order. `sections` lists the ids of the `<section>`s a
 * view shows; `null` means every section. Every section id of the page is
 * in exactly one of the four named views (pinned by
 * tests/web_ui/test_web_ui_admin_views.py). */
export const VIEWS = [
  { id: "status", label: "وضعیت", sections: ["sec-maintenance", "sec-health"] },
  { id: "quality", label: "کیفیت و استفاده", sections: ["sec-stats", "sec-feedback", "sec-quality", "sec-usage"] },
  { id: "access", label: "دسترسی و کلیدها", sections: ["sec-access-requests", "sec-keys", "sec-auth"] },
  { id: "data", label: "داده و پیکربندی", sections: ["sec-cache", "sec-config", "sec-schema", "sec-vocab"] },
  { id: "all", label: "همه", sections: null },
];

export const DEFAULT_VIEW = "status";
export const STORAGE_KEY = "lsa-admin-view";

/**
 * The id of the named view that holds section *sectionId*, or null when
 * no named view does (the summary rail, `#overview`, is in every view).
 *
 * @param {string} sectionId
 * @returns {string|null}
 */
export function viewForSection(sectionId) {
  for (const v of VIEWS) {
    if (v.sections && v.sections.includes(sectionId)) return v.id;
  }
  return null;
}

/**
 * True when *viewId* names a view.
 *
 * @param {unknown} viewId
 * @returns {boolean}
 */
export function isView(viewId) {
  return VIEWS.some((v) => v.id === viewId);
}

/**
 * Show the sections of *viewId* and hide the others.
 *
 * @param {ParentNode} root — the document (or any node holding the page).
 * @param {string} viewId — an id from VIEWS; an unknown id shows every
 *   section, so a bad value can never leave the page empty.
 * @returns {string} the view actually applied.
 */
export function applyView(root, viewId) {
  const view = VIEWS.find((v) => v.id === viewId) || VIEWS.find((v) => v.id === "all");
  const shown = view.sections ? new Set(view.sections) : null;

  for (const sec of root.querySelectorAll("section.admin-section")) {
    sec.hidden = shown ? !shown.has(sec.id) : false;
  }
  // A two-column wrapper with nothing left to show would still take its
  // gap in the page; with one section left, the grid's auto-fit gives that
  // section the full width.
  for (const wrap of root.querySelectorAll(".admin-columns")) {
    const kids = wrap.querySelectorAll("section.admin-section");
    wrap.hidden = kids.length > 0 && [...kids].every((k) => k.hidden);
  }
  for (const a of root.querySelectorAll(".admin-jump a[href^='#']")) {
    const id = a.getAttribute("href").slice(1);
    const owner = viewForSection(id);
    a.hidden = Boolean(shown) && owner !== null && !shown.has(id);
  }
  for (const b of root.querySelectorAll(".admin-view-btn")) {
    const on = b.dataset.view === view.id;
    b.setAttribute("aria-pressed", String(on));
    b.classList.toggle("on", on);
  }
  return view.id;
}

/**
 * Mark the view buttons that need attention (a failed check, maintenance
 * on, open feedback). A mark is a dot plus a visually hidden word, so it
 * is not carried by colour alone and a screen reader hears it.
 *
 * @param {ParentNode} root
 * @param {Record<string, boolean>} flags — view id → needs attention.
 */
export function markAttention(root, flags) {
  for (const b of root.querySelectorAll(".admin-view-btn")) {
    const on = Boolean(flags[b.dataset.view]);
    b.classList.toggle("attn", on);
    const sr = b.querySelector(".admin-view-attn");
    if (sr) sr.textContent = on ? " (نیاز به توجه)" : "";
  }
}

/**
 * The view to open with: the one holding the section in *hash* when it
 * names one, else the remembered view, else DEFAULT_VIEW.
 *
 * @param {string} hash — location.hash, with or without "#".
 * @param {string|null} remembered — the value read from localStorage.
 * @returns {string}
 */
export function initialView(hash, remembered) {
  const id = String(hash || "").replace(/^#/, "");
  const owner = id ? viewForSection(id) : null;
  if (owner) return owner;
  if (isView(remembered)) return remembered;
  return DEFAULT_VIEW;
}

/** Read the remembered view; null when storage is unavailable. */
export function readRememberedView(storage) {
  try { return storage ? storage.getItem(STORAGE_KEY) : null; } catch { return null; }
}

/** Remember *viewId*; storage that throws (private mode) is ignored. */
export function rememberView(storage, viewId) {
  try { if (storage) storage.setItem(STORAGE_KEY, viewId); } catch { /* per-browser convenience only */ }
}

/**
 * Build the view buttons and put them at the start of *nav*.
 *
 * @param {HTMLElement} nav — the sticky `.admin-jump` nav.
 * @param {(viewId: string) => void} onSelect
 * @returns {HTMLElement} the group of buttons.
 */
export function renderViewSwitch(nav, onSelect) {
  const group = document.createElement("div");
  group.className = "admin-views";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "نما");
  for (const v of VIEWS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "admin-view-btn";
    b.dataset.view = v.id;
    b.setAttribute("aria-pressed", "false");
    b.textContent = v.label;
    const sr = document.createElement("span");
    sr.className = "admin-view-attn sr-only";
    b.appendChild(sr);
    b.addEventListener("click", () => onSelect(v.id));
    group.appendChild(b);
  }
  nav.insertBefore(group, nav.firstChild);
  return group;
}
