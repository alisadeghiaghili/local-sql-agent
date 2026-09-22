/* web/admin/main.js — admin panel bootstrap and rendering.
 *
 * Started as a pure dashboard (docs/admin-panel-architecture.md / the
 * phase-1 spec): every phase-1/2/3 card only ever reads. Phase 4 added
 * the first deliberate exception (feedback resolution) and phase 6 adds
 * three more, each a narrow, closed-set operations action, never a
 * free-form write surface (§3.1's line the architecture draws): the
 * maintenance-mode toggle, a manual vocabulary-column refresh, and
 * clearing the query-result cache. See web/admin/admin.js's own comments
 * on each corresponding method for why each stays inside that line.
 *
 * Reuses web/js/apikey.js (credentials) and web/js/state.js (theme /
 * backend base-URL persistence) unchanged -- an admin key is just a key
 * with the `admin` capability, so this page shares the exact same
 * localStorage-backed key store the analyst UI (web/) already uses,
 * rather than inventing a second one.
 */

"use strict";

import { AdminApi, AdminUnauthorizedError, AdminForbiddenError, AdminApiError } from "./admin.js";
import { renderAccessRequestsList } from "./access-requests.js";
import { getApiKey, setApiKey, clearApiKey, hasApiKey } from "../js/apikey.js";
import { state, loadPersisted, persistTheme, persistBaseUrl, applyTheme } from "../js/state.js";

const $ = (id) => document.getElementById(id);

/* Every card this page fetches and re-renders, one at a time (see
 * refreshOne below). Declared here, before the top-level bootstrap calls
 * below, because those calls run refreshAll() synchronously as part of
 * page load -- a `const` referenced before its own declaration line has
 * executed is a ReferenceError (the temporal dead zone), not a "not yet
 * defined" value, so this array cannot sit below the code that runs
 * before the module finishes its first pass. */
const CARDS = [
  "summary", "health", "cache", "config", "feedback", "accessRequests",
  "maintenance", "keys", "schemaDrift", "vocabulary", "usage", "authFailures",
];

/* Cards whose 403 is an ordinary, expected outcome rather than a wrong
 * credential, and so must render inside the card instead of raising the
 * page-level "this key is not an admin key" banner.
 *
 * The panel's sections do not share one capability: `require_admin`
 * serves four of them and `require_operations_or_security` the other six,
 * so a key holding only some of the three legitimately sees a 403 on the
 * rest. Before phase 7 that produced a banner claiming the key was not an
 * admin key at all -- which is false, and points at the wrong fix.
 *
 * Maps each such card to the ONE capability it needs -- "keys" wants
 * `operations`, "accessRequests" (ADR-004 part 1, security-only both for
 * listing and acting) wants `security`; the two must not share one
 * hardcoded message, or one of them would tell the operator to issue the
 * wrong kind of key. */
const CARDS_WHERE_403_IS_A_CAPABILITY_GAP = new Map([
  ["keys", "operations"],
  ["accessRequests", "security"],
]);

loadPersisted();
applyTheme();
let api = new AdminApi(state.baseUrl);

wireTopbar();
tickClock();
setInterval(tickClock, 1000);
refreshAll();

/* ── Topbar: theme, backend base URL, API key ─────────────────────── */
function wireTopbar() {
  $("theme-toggle").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    const next = order[(order.indexOf(state.theme) + 1) % order.length];
    persistTheme(next);
    applyTheme();
    updateThemeLabel();
  });
  updateThemeLabel();

  $("live-base-input").value = state.baseUrl;
  $("live-base-connect").addEventListener("click", () => {
    const val = $("live-base-input").value.trim();
    if (!val) return;
    state.baseUrl = val.replace(/\/+$/, "");
    persistBaseUrl(state.baseUrl);
    api = new AdminApi(state.baseUrl);
    refreshAll();
  });

  $("live-key-save").addEventListener("click", () => {
    const val = $("live-key-input").value;
    if (!val.trim()) return;
    setApiKey(val);
    $("live-key-input").value = "";
    updateKeyStatus();
    hideForbiddenBanner();
    refreshAll();
  });

  $("live-key-change").addEventListener("click", () => {
    $("live-key-entry").hidden = false;
    $("live-key-change").hidden = true;
    $("live-key-input").focus();
  });

  $("live-key-clear").addEventListener("click", () => {
    clearApiKey();
    $("live-key-input").value = "";
    updateKeyStatus();
    refreshAll();
  });
  updateKeyStatus();

  $("admin-refresh-all").addEventListener("click", refreshAll);
  document.querySelectorAll("[data-refresh]").forEach((btn) => {
    btn.addEventListener("click", () => refreshOne(btn.dataset.refresh));
  });
}

function updateThemeLabel() {
  const labels = { system: "پوسته: سیستم", light: "پوسته: روشن", dark: "پوسته: تیره" };
  $("theme-toggle-label").textContent = labels[state.theme];
}

/* Same three states as the analyst UI (web/js/main.js) -- see that
 * function's comment. The panel's own version of the problem was worse:
 * an admin key is entered once and then every card on the page depends on
 * it, so an always-empty password box beside ten loaded cards is a
 * standing invitation to re-enter a key that was never lost. */
function updateKeyStatus(rejected = false) {
  const el = $("live-key-status");
  const entry = $("live-key-entry");
  const change = $("live-key-change");
  const clear = $("live-key-clear");
  const stored = hasApiKey();

  if (rejected) {
    el.textContent = "کلید: رد شد";
    el.className = "live-key-status unset";
  } else if (stored) {
    el.textContent = "کلید: ذخیره شده ✓";
    el.className = "live-key-status set";
  } else {
    el.textContent = "کلید: تنظیم نشده";
    el.className = "live-key-status unset";
  }

  const showEntry = rejected || !stored;
  entry.hidden = !showEntry;
  change.hidden = showEntry;
  clear.hidden = !stored;
}

function tickClock() {
  const el = $("foot-time");
  if (el) el.textContent = new Date().toLocaleString("fa-IR");
}

/* ── Forbidden banner: "this key is not an admin key" ───────────────
 * The phase-1 spec is explicit: a 403 must say this plainly rather than
 * leaving every card showing an empty/stuck loading state. */
function showForbiddenBanner(message) {
  const el = $("admin-forbidden-banner");
  el.textContent = message;
  el.hidden = false;
}

function hideForbiddenBanner() {
  $("admin-forbidden-banner").hidden = true;
}

function showNotice(kind, message) {
  const area = $("admin-notice-area");
  area.innerHTML = "";
  const div = document.createElement("div");
  div.className = `admin-banner admin-banner-${kind === "error" ? "error" : "warn"}`;
  div.textContent = message;
  area.appendChild(div);
}

function clearNotice() {
  $("admin-notice-area").innerHTML = "";
}

/* ── Fetch + render, one card at a time ─────────────────────────────
 * Each card fails independently: a 403 on one call (all four will 403
 * together, since it is the same key) still renders the others' error
 * state instead of the whole page going blank, and a network error on
 * one endpoint does not block the rest. CARDS itself is declared near
 * the top of this file, above the bootstrap calls that run it first. */

async function refreshAll() {
  hideForbiddenBanner();
  clearNotice();
  await Promise.all(CARDS.map(refreshOne));
  updateSummaryRail();
  markLastUpdated();
}

/** Snapshot the four rail numbers from the last successful payloads. */
const _rail = { maintenance: null, health: null, feedback: null, cache: null };

function updateSummaryRail() {
  setRailItem("rail-maintenance", _rail.maintenance);
  setRailItem("rail-checks", _rail.health);
  setRailItem("rail-feedback", _rail.feedback);
  setRailItem("rail-cache", _rail.cache);
}

function setRailItem(id, data) {
  const el = $(id);
  if (!el) return;
  const val = el.querySelector(".rail-val");
  const sub = el.querySelector(".rail-sub");
  el.classList.remove("good", "warn", "crit");
  if (!data) {
    val.textContent = "—";
    sub.textContent = "—";
    return;
  }
  if (id === "rail-maintenance") {
    const on = !!(data.enabled || data.active || data.on);
    val.textContent = on ? "روشن" : "خاموش";
    sub.textContent = on ? (data.note || "تحلیل‌گران متوقف") : "تحلیل‌گران فعال";
    el.classList.add(on ? "crit" : "good");
  } else if (id === "rail-checks") {
    const checks = data.checks || [];
    const failed = checks.filter((c) => c.status === "FAIL").length;
    val.textContent = failed ? `${failed} / ${checks.length}` : `${checks.length}`;
    sub.textContent = failed ? `${failed} ناموفق` : "همه موفق";
    el.classList.add(failed ? "crit" : "good");
  } else if (id === "rail-feedback") {
    const open = data.open ?? data.total_open ?? (data.stats && data.stats.open) ?? null;
    const n = open === null ? (Array.isArray(data.feedback) ? data.feedback.length : "—") : open;
    val.textContent = String(n);
    sub.textContent = "باز";
    el.classList.add(Number(n) > 0 ? "warn" : "good");
  } else if (id === "rail-cache") {
    const size = data.size ?? data.entries ?? "—";
    val.textContent = String(size);
    sub.textContent = "ورودی";
    el.classList.add("good");
  }
}

function markLastUpdated() {
  const el = $("admin-last-updated");
  if (!el) return;
  const d = new Date();
  el.textContent = `آخرین: ${d.toLocaleTimeString("fa-IR")}`;
}

/* Auto-refresh every 30s — one cadence instead of ↻ on every section.
   Manual buttons remain for a single card after a write action. */
const AUTO_REFRESH_MS = 30_000;
setInterval(() => {
  if (document.hidden) return;
  refreshAll();
}, AUTO_REFRESH_MS);

/* Scroll-spy for the sticky jump nav so the active section is obvious
 * while scrolling ten cards. IntersectionObserver, not a scroll handler. */
(function wireJumpNavSpy() {
  const nav = $("admin-jump");
  if (!nav || typeof IntersectionObserver !== "function") return;
  const links = [...nav.querySelectorAll("a[href^='#']")];
  const map = new Map();
  for (const a of links) {
    const id = a.getAttribute("href").slice(1);
    const sec = document.getElementById(id);
    if (sec) map.set(sec, a);
  }
  if (!map.size) return;
  const obs = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        links.forEach((a) => a.classList.remove("on"));
        const a = map.get(e.target);
        if (a) a.classList.add("on");
      }
    },
    { rootMargin: "-20% 0px -60% 0px", threshold: 0 },
  );
  map.forEach((_a, sec) => obs.observe(sec));
})();

async function refreshOne(name) {
  const body = $(`${name}-body`);
  const btn = document.querySelector(`[data-refresh="${name}"]`);
  if (btn) btn.disabled = true;
  body.innerHTML = '<p class="admin-loading">در حال بارگذاری…</p>';
  try {
    if (name === "summary") {
      const includeExamples = $("include-examples-toggle").checked;
      renderSummary(await api.summary(includeExamples));
    } else if (name === "health") {
      const payload = await api.healthChecks();
      _rail.health = payload;
      renderHealth(payload);
    } else if (name === "cache") {
      const payload = await api.cache();
      _rail.cache = payload;
      renderCache(payload);
    } else if (name === "config") {
      renderConfig(await api.config());
    } else if (name === "feedback") {
      const status = $("feedback-status-filter").value;
      const [stats, list] = await Promise.all([api.feedbackStats(), api.feedbackList(status)]);
      _rail.feedback = { ...(stats || {}), ...(list || {}) };
      renderFeedback(stats, list.feedback || []);
    } else if (name === "accessRequests") {
      const payload = await api.accessRequestsList();
      renderAccessRequestsCard(payload.access_requests || []);
    } else if (name === "maintenance") {
      const payload = await api.maintenanceState();
      _rail.maintenance = payload;
      renderMaintenance(payload);
    } else if (name === "keys") {
      renderKeys(await loadKeysCard());
    } else if (name === "schemaDrift") {
      renderSchemaDrift(await api.schemaDrift());
    } else if (name === "vocabulary") {
      renderVocabulary(await api.vocabularyStatus());
    } else if (name === "usage") {
      renderUsage(await api.usage());
    } else if (name === "authFailures") {
      renderAuthFailures(await api.authFailures());
    }
    hideForbiddenBanner();
  } catch (err) {
    body.innerHTML = "";
    if (err instanceof AdminForbiddenError && CARDS_WHERE_403_IS_A_CAPABILITY_GAP.has(name)) {
      const capability = CARDS_WHERE_403_IS_A_CAPABILITY_GAP.get(name);
      body.innerHTML =
        `<p class="admin-loading">این بخش به نقش <code>${capability}</code> نیاز دارد و کلید شما آن را ندارد. ` +
        `کلیدی با <code>--${capability}</code> (یا <code>--full-admin</code>) صادر کنید — ` +
        `<code>python -m scripts.issue_api_key</code>.</p>`;
    } else if (err instanceof AdminForbiddenError) {
      showForbiddenBanner(
        "این کلید، کلید مدیریتی نیست — این کلید معتبر است ولی دسترسی مدیریتی ندارد. " +
        (err.message || ""),
      );
    } else if (err instanceof AdminUnauthorizedError) {
      // Marks the stored key as rejected rather than only saying "enter a
      // key": with a key already stored, that message read as "you are
      // signed out" when the real state was "the server said no to what
      // you have". The key itself is kept -- it is unrecoverable, and a
      // 401 can be a restarted server rather than a bad key.
      updateKeyStatus(hasApiKey());
      showNotice(
        "error",
        hasApiKey()
          ? "سرور کلید ذخیره‌شده را نپذیرفت (۴۰۱). کلید پاک نشده — اگر سرور تازه ری‌استارت شده دوباره امتحان کنید، وگرنه کلید مدیریتی تازه‌ای وارد کنید."
          : "کلید API وارد نشده است. یک کلید مدیریتی وارد کنید.",
      );
    } else if (err instanceof AdminApiError) {
      body.innerHTML = `<p class="admin-loading">خطا: ${escapeHtml(err.message)}</p>`;
    } else {
      body.innerHTML = `<p class="admin-loading">خطای غیرمنتظره: ${escapeHtml(String(err))}</p>`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

$("include-examples-toggle").addEventListener("change", () => refreshOne("summary"));
$("feedback-status-filter").addEventListener("change", () => refreshOne("feedback"));

/* ── Cache clear: say the cost BEFORE doing it (spec §5) ────────────── */
$("cache-clear-btn").addEventListener("click", async () => {
  const stats = _lastCacheStats || (await api.cache().catch(() => null));
  const size = stats ? fmtNum(stats.size ?? 0) : "نامشخص";
  const proceed = window.confirm(
    `با پاک کردن کش، ${size} ورودی ذخیره‌شده حذف می‌شود و درخواست‌های بعدی هزینهٔ کامل (بدون کش) خواهند داشت. ادامه می‌دهید؟`,
  );
  if (!proceed) return;
  try {
    await api.cacheClear();
    showNotice("warn", "کش پاک شد.");
    await refreshOne("cache");
  } catch (err) {
    showNotice("error", `پاک کردن کش ناموفق بود: ${err.message || err}`);
  }
});

/* ── Renderers ───────────────────────────────────────────────────── */

function escapeHtml(s) {
  /* This used to be the `textContent` -> `innerHTML` idiom, which only
   * encodes what matters inside a *text node* (&, < and >). Two call
   * sites (renderKeys' data-pid/data-hash, the vocabulary card's
   * data-vocab-refresh) used its output to build an HTML *attribute*
   * instead, where `"` and `'` are exactly the characters that matter --
   * the audit's finding 2 reproduced a real onmouseover attribute break-
   * out through that gap. Encode all five characters explicitly so this
   * function is safe wherever the next caller puts it, text node or
   * attribute, rather than being correct only for the position it was
   * first written for. */
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Format a rate that may arrive as 0..1, as a percentage, or as null. */
function asPercent(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  if (Number.isNaN(n)) return null;
  return n <= 1 ? n * 100 : n;
}

function statTile({ label, value, unit, sub, cls }) {
  const u = unit ? `<span class="admin-stat-unit">${escapeHtml(unit)}</span>` : "";
  const s = sub ? `<span class="admin-stat-sub" dir="ltr">${escapeHtml(sub)}</span>` : "";
  return (
    `<div class="admin-stat ${cls || ""}">` +
    `<span class="admin-stat-label">${escapeHtml(label)}</span>` +
    `<span class="admin-stat-value">${escapeHtml(value)}${u}</span>${s}</div>`
  );
}

function renderSummary(report) {
  const body = $("summary-body");
  const out = [];

  const modeClass = report.mode === "aggregate_with_examples" ? "examples" : "";
  const modeLabel = report.mode === "aggregate_with_examples"
    ? "شامل نمونهٔ سؤال‌های واقعی — این خروجی را بی‌ملاحظه از سرور خارج نکنید"
    : "تجمیعی — بدون سؤال یا SQL خام";
  out.push(`<span class="admin-mode-pill ${modeClass}">${escapeHtml(modeLabel)}</span>`);

  // ── The figures, leading. ────────────────────────────────────────────
  const tiles = [];
  tiles.push(statTile({ label: "پرس‌وجوها", value: fmtNum(report.record_count ?? 0) }));

  const lat = report.latency?.overall_ms;
  if (lat && lat.count) {
    tiles.push(statTile({
      label: "تأخیر p50", value: fmtNum(Math.round(lat.p50)), unit: "ms",
      sub: `p95 ${Math.round(lat.p95)} · p99 ${Math.round(lat.p99)}`,
    }));
  }

  const ft = report.failure_taxonomy;
  if (ft && (ft.success_count || ft.failure_count)) {
    const total = ft.success_count + ft.failure_count;
    const rate = total ? (ft.success_count / total) * 100 : 0;
    tiles.push(statTile({
      label: "نرخ موفقیت", value: rate.toFixed(1), unit: "٪",
      sub: `${fmtNum(ft.failure_count)} ناموفق از ${fmtNum(total)}`,
      cls: rate < 90 ? "is-degraded" : "",
    }));
  }

  const cb = report.cache_behaviour || {};
  const t0 = asPercent(cb.t0_rate);
  if (t0 !== null) {
    tiles.push(statTile({ label: "پاسخ از کش", value: t0.toFixed(1), unit: "٪" }));
  }

  // The keystone: when prefix reuse breaks, nothing errors and every
  // request pays full prefill. See admin.css's header.
  const prefix = asPercent(cb.prefix_cache_hit_rate);
  if (prefix !== null) {
    tiles.push(statTile({
      label: "بازاستفادهٔ prefix",
      value: prefix.toFixed(1), unit: "٪",
      sub: prefix < 50 ? "پایین — هر درخواست prefill کامل می‌دهد" : "سالم",
      cls: prefix < 50 ? "is-degraded" : "is-keystone",
    }));
  }

  const cr = report.correction_rounds;
  if (cr && cr.total !== undefined) {
    tiles.push(statTile({
      label: "دورهای اصلاح", value: fmtNum(cr.total),
      sub: cr.mean !== undefined ? `میانگین ${Number(cr.mean).toFixed(2)}` : "",
    }));
  }

  out.push(`<div class="admin-stats">${tiles.join("")}</div>`);

  const range = report.time_range || {};
  if (range.start || range.end) {
    out.push(
      `<p class="admin-loading" dir="ltr">${escapeHtml(range.start ?? "—")} → ${escapeHtml(range.end ?? "—")}</p>`,
    );
  }

  if (report.records_by_model && Object.keys(report.records_by_model).length) {
    out.push('<p class="admin-section-title">به تفکیک مدل</p>');
    out.push(twoColumnTable("مدل", "تعداد", Object.entries(report.records_by_model)));
  }

  const fr = report.finish_reason_distribution;
  if (fr && Object.keys(fr).length) {
    out.push('<p class="admin-section-title">دلیل پایان تولید</p>');
    out.push(twoColumnTable("finish_reason", "تعداد", Object.entries(fr)));
  }

  body.innerHTML = out.join("");
}

function fmtNum(n) {
  return Number(n).toLocaleString("fa-IR");
}

function twoColumnTable(headA, headB, entries) {
  const rows = entries.map(
    ([k, v]) =>
      `<tr><td dir="ltr">${escapeHtml(k)}</td><td class="num">${escapeHtml(fmtNum(v))}</td></tr>`,
  );
  return (
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    `<th>${escapeHtml(headA)}</th><th>${escapeHtml(headB)}</th>` +
    `</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
  );
}

function kv(label, value) {
  return `<dt>${escapeHtml(label)}</dt><dd dir="ltr">${escapeHtml(value)}</dd>`;
}

function renderHealth(payload) {
  const body = $("health-body");
  const checks = payload.checks || [];
  if (!checks.length) {
    body.innerHTML = '<p class="admin-loading">هیچ بررسی‌ای ثبت نشده است.</p>';
    return;
  }

  const failed = checks.filter((c) => c.status === "FAIL").length;
  const passed = checks.filter((c) => c.status === "PASS").length;

  // The count comes first and names the failures, so an operator who reads
  // one line has read the thing that matters.
  const summary = failed
    ? `<p class="admin-rail-summary has-failures"><strong>${fmtNum(failed)} بررسی ناموفق</strong> از ${fmtNum(checks.length)}</p>`
    : `<p class="admin-rail-summary"><strong>هر ${fmtNum(checks.length)} بررسی موفق</strong></p>`;

  // Failures first: sorting by severity means the thing needing action is
  // never below the fold on a narrow screen.
  const order = { FAIL: 0, SKIP: 1, PASS: 2 };
  const sorted = checks.slice().sort(
    (a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3),
  );

  const cards = sorted.map((c) => {
    const cls = c.status === "PASS" ? "pass" : c.status === "FAIL" ? "fail" : "skip";
    const mark = c.status === "PASS" ? "✓" : c.status === "FAIL" ? "✕" : "–";
    const detail = c.detail
      ? `<span class="admin-check-detail">${escapeHtml(c.detail)}</span>`
      : "";
    return (
      `<div class="admin-check ${cls}">` +
      `<span class="admin-check-mark" aria-hidden="true">${mark}</span>` +
      `<span><span class="admin-check-name">${escapeHtml(c.name)}</span>` +
      `<span class="admin-status-pill admin-status-${cls}" style="margin-inline-start:6px">${escapeHtml(c.status)}</span>` +
      detail +
      "</span></div>"
    );
  });

  body.innerHTML = summary + cards.join("");
  void passed;
}

/** Admin panel phase 6 §5: the panel must show what clearing will cost
 * BEFORE the operator clicks clear, not only echo it back afterward.
 * Kept from the most recent GET /admin/cache (phase 1, read-only) so the
 * confirm dialog below is never stale by more than one refresh cycle. */
let _lastCacheStats = null;

function renderCache(stats) {
  _lastCacheStats = stats;
  const body = $("cache-body");
  const hits = Number(stats.hits ?? 0);
  const misses = Number(stats.misses ?? 0);
  const total = hits + misses;
  const rate = total ? (hits / total) * 100 : null;

  const tiles = [
    statTile({ label: "اندازهٔ فعلی", value: fmtNum(stats.size ?? 0), sub: stats.max_size !== undefined ? `از ${fmtNum(stats.max_size)}` : "" }),
  ];
  if (rate !== null) {
    tiles.push(statTile({ label: "نرخ اصابت", value: rate.toFixed(1), unit: "٪", sub: `${fmtNum(hits)} / ${fmtNum(total)}` }));
  }

  const rows = [kv("فعال", stats.enabled ? "بله" : "خیر")];
  rows.push(kv("evictions", fmtNum(stats.evictions ?? 0)));
  if (stats.sql_index_size !== undefined) rows.push(kv("نمایهٔ SQL", fmtNum(stats.sql_index_size)));
  if (stats.ttl_seconds !== undefined) rows.push(kv("TTL", `${fmtNum(stats.ttl_seconds)} s`));

  body.innerHTML =
    `<div class="admin-stats">${tiles.join("")}</div>` +
    `<dl class="admin-kv">${rows.join("")}</dl>`;
}

function renderConfig(payload) {
  const body = $("config-body");
  const files = payload.files || [];
  const rows = files.map((f) => {
    const cls = f.loaded ? "pass" : "fail";
    const status = f.loaded ? "بارگذاری شد" : "خطا";
    return `<tr>
      <td dir="ltr">${escapeHtml(f.file)}</td>
      <td><span class="admin-status-pill admin-status-${cls}">${escapeHtml(status)}</span></td>
      <td class="num">${f.count === undefined || f.count === null ? "—" : escapeHtml(fmtNum(f.count))}</td>
      <td>${escapeHtml(f.error || "")}</td>
    </tr>`;
  });
  body.innerHTML = (
    `<p class="admin-loading" dir="ltr">${escapeHtml(payload.project_config_dir || "")}</p>` +
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    "<th>فایل</th><th>وضعیت</th><th>تعداد ورودی</th><th>خطا</th></tr></thead><tbody>" +
    rows.join("") +
    "</tbody></table></div>"
  );
}

/* ── Feedback triage (admin panel phase 4, spec §3, §5) ──────────────
 * The one card on this page that both reads AND writes -- see
 * admin.js's own comment on resolveFeedback for why that write is
 * narrow (a fixed, closed-set decision) rather than the free-form write
 * surface §3.1 of the architecture forbids. */

const RESOLUTION_OUTCOMES = ["alias_fix", "rule_fix", "golden_case", "not_a_defect"];
const RESOLUTION_LABELS = {
  alias_fix: "اصلاح مترادف/نام مستعار",
  rule_fix: "اصلاح قاعدهٔ کسب‌وکار",
  golden_case: "تبدیل به پروندهٔ طلایی",
  not_a_defect: "نقص نیست",
};
const CATEGORY_LABELS = {
  wrong_number: "عدد اشتباه",
  different_question: "پاسخ به سؤال دیگر",
  wrong_filter_or_period: "فیلتر/بازهٔ زمانی اشتباه",
  other: "سایر",
};

function renderFeedback(stats, rows) {
  const statsBody = $("feedback-stats-body");
  const tiles = [
    statTile({ label: "کل بازخوردها", value: fmtNum(stats.flags_total ?? 0) }),
    statTile({
      label: "باز", value: fmtNum(stats.flags_open ?? 0),
      cls: stats.flags_open ? "is-degraded" : "",
    }),
    statTile({
      label: "اندازهٔ مجموعهٔ طلایی", value: fmtNum(stats.golden_set_size ?? 0),
      sub: stats.golden_set_pending ? `${fmtNum(stats.golden_set_pending)} در انتظار پاسخ` : "همه دارای پاسخ تأییدشده",
    }),
  ];
  if (stats.baseline) {
    tiles.push(statTile({
      label: "دقت آخرین baseline",
      value: Number(stats.baseline.accuracy_pct).toFixed(1), unit: "٪",
      sub: `${escapeHtml(stats.baseline.mode)} · ${escapeHtml(stats.baseline.generated_at || "")}`,
    }));
  } else {
    tiles.push(statTile({ label: "دقت آخرین baseline", value: "—", sub: "هنوز baseline ثبت نشده" }));
  }
  statsBody.innerHTML = `<div class="admin-stats">${tiles.join("")}</div>`;

  const body = $("feedback-body");
  if (!rows.length) {
    body.innerHTML = '<p class="admin-loading">بازخوردی یافت نشد.</p>';
    return;
  }
  body.innerHTML = rows.map(feedbackRowHtml).join("");
  body.querySelectorAll("[data-resolve-id]").forEach((btn) => {
    btn.addEventListener("click", () => resolveFeedbackRow(btn));
  });
}

function feedbackRowHtml(row) {
  const audit = row.audit;
  const cls = row.status === "resolved" ? "pass" : "";
  const questionLine = audit
    ? `<p class="admin-check-detail">${escapeHtml(audit.question || "")}</p>`
    : '<p class="admin-check-detail">(دیگر قابل بازیابی از گزارش ممیزی نیست)</p>';
  const sqlLine = audit && audit.generated_sql
    ? `<pre class="admin-loading" dir="ltr" style="white-space:pre-wrap">${escapeHtml(audit.generated_sql)}</pre>`
    : "";
  const guardLine = audit && audit.guard
    ? `<span class="admin-status-pill admin-status-${audit.guard.verdict === "allowed" ? "pass" : "fail"}">${escapeHtml(audit.guard.verdict || "")}</span>`
    : "";

  const noteLine = row.note ? `<p class="admin-check-detail">یادداشت تحلیل‌گر: ${escapeHtml(row.note)}</p>` : "";

  let actionArea;
  if (row.status === "resolved") {
    actionArea = (
      `<p class="admin-check-detail">نتیجه: ${escapeHtml(RESOLUTION_LABELS[row.resolution_outcome] || row.resolution_outcome || "")}` +
      (row.resolution_note ? ` — ${escapeHtml(row.resolution_note)}` : "") +
      `</p>`
    );
  } else {
    const options = RESOLUTION_OUTCOMES
      .map((o) => `<option value="${o}">${escapeHtml(RESOLUTION_LABELS[o])}</option>`)
      .join("");
    actionArea = (
      `<div class="admin-feedback-actions" data-feedback-id="${row.feedback_id}">` +
      `<select class="admin-toggle fb-outcome">${options}</select>` +
      `<input type="text" class="fb-note" placeholder="یادداشت (برای «نقص نیست» الزامی است)">` +
      `<button class="admin-btn-refresh" data-resolve-id="${row.feedback_id}" type="button">ثبت نتیجه</button>` +
      `</div>`
    );
  }

  return (
    `<div class="admin-check ${cls}" data-row-id="${row.feedback_id}">` +
    `<span class="admin-check-mark" aria-hidden="true">${row.status === "resolved" ? "✓" : "●"}</span>` +
    `<span>` +
    `<span class="admin-check-name">#${row.feedback_id} · ${escapeHtml(CATEGORY_LABELS[row.category] || row.category)}</span> ` +
    guardLine +
    questionLine + sqlLine + noteLine + actionArea +
    `</span></div>`
  );
}

async function resolveFeedbackRow(btn) {
  const wrap = btn.closest("[data-feedback-id]");
  const feedbackId = wrap.dataset.feedbackId;
  const outcome = wrap.querySelector(".fb-outcome").value;
  const note = wrap.querySelector(".fb-note").value;
  btn.disabled = true;
  try {
    await api.resolveFeedback(feedbackId, { outcome, note });
    await refreshOne("feedback");
  } catch (err) {
    showNotice("error", `ثبت نتیجه ناموفق بود: ${err.message || err}`);
    btn.disabled = false;
  }
}

/* ── ADR-004 part 1: "Request access" triage queue -- security only ──
 * The DOM building itself lives in access-requests.js (renderAccessRequestsList),
 * kept separate and import-testable under Node the same way
 * web/js/render/feedback.js is -- see that module's own docstring. This
 * function is only the fetch-card glue: which host element, and what the
 * approve/deny buttons actually do once clicked. */

function renderAccessRequestsCard(rows) {
  const body = $("accessRequests-body");
  renderAccessRequestsList(body, rows, {
    onApprove: (requestId, btn) => approveAccessRequestRow(requestId, btn),
    onDeny: (requestId, reason, btn) => denyAccessRequestRow(requestId, reason, btn),
  });
}

async function approveAccessRequestRow(requestId, btn) {
  btn.disabled = true;
  try {
    await api.approveAccessRequest(requestId);
    await refreshOne("accessRequests");
  } catch (err) {
    showNotice("error", `تأیید درخواست دسترسی ناموفق بود: ${err.message || err}`);
    btn.disabled = false;
  }
}

async function denyAccessRequestRow(requestId, reason, btn) {
  if (!reason || !reason.trim()) {
    showNotice("error", "برای رد درخواست، نوشتن دلیل الزامی است.");
    return;
  }
  btn.disabled = true;
  try {
    await api.denyAccessRequest(requestId, reason.trim());
    await refreshOne("accessRequests");
  } catch (err) {
    showNotice("error", `رد درخواست دسترسی ناموفق بود: ${err.message || err}`);
    btn.disabled = false;
  }
}

/* ── Admin panel phase 6: maintenance mode ───────────────────────────
 * A switch, not a trap (docs/admin-panel-architecture.md / phase 6
 * spec §1) -- this card is the one place on the page that can change
 * whether analyst queries are being answered at all, so its state is
 * rendered as a severity banner, the same visual language the
 * deployment-checks rail already uses for a failure. */

function renderMaintenance(state) {
  const body = $("maintenance-body");
  const cls = state.active ? "fail" : "pass";
  const label = state.active ? "روشن — پرس‌وجوهای جدید رد می‌شوند" : "خاموش — سامانه عادی کار می‌کند";
  const rows = [];
  if (state.note) rows.push(kv("یادداشت", state.note));
  if (state.since) rows.push(kv("از زمان", state.since));
  if (state.actor_principal_id) rows.push(kv("توسط", state.actor_principal_id));

  const noteField = state.active
    ? ""
    : `<input type="text" id="maintenance-note-input" class="fb-note" placeholder="یادداشت برای تحلیل‌گران (اختیاری)">`;
  const btnLabel = state.active ? "خاموش کردن" : "روشن کردن";

  body.innerHTML =
    `<p class="admin-rail-summary ${state.active ? "has-failures" : ""}"><strong>${escapeHtml(label)}</strong></p>` +
    (rows.length ? `<dl class="admin-kv">${rows.join("")}</dl>` : "") +
    `<div class="admin-feedback-actions">${noteField}` +
    `<button class="admin-btn-refresh" id="maintenance-toggle-btn" type="button">${escapeHtml(btnLabel)}</button>` +
    `</div>`;

  $("maintenance-toggle-btn").addEventListener("click", async () => {
    const btn = $("maintenance-toggle-btn");
    btn.disabled = true;
    try {
      if (state.active) {
        await api.setMaintenance(false);
      } else {
        const noteInput = $("maintenance-note-input");
        await api.setMaintenance(true, noteInput ? noteInput.value : "");
      }
      await refreshOne("maintenance");
    } catch (err) {
      showNotice("error", `تغییر حالت تعمیر ناموفق بود: ${err.message || err}`);
      btn.disabled = false;
    }
  });
}

/* ── Admin panel phase 6: schema drift -- read-only, proposes nothing ── */

function renderSchemaDrift(report) {
  const body = $("schemaDrift-body");
  const noDrift =
    (!report.warehouse_only || !report.warehouse_only.length) &&
    (!report.schema_only || !report.schema_only.length) &&
    (!report.type_changed || !report.type_changed.length);

  const out = [];
  if (noDrift) {
    out.push('<p class="admin-rail-summary"><strong>انحرافی یافت نشد</strong></p>');
  } else {
    if (report.warehouse_only && report.warehouse_only.length) {
      out.push('<p class="admin-section-title">فقط در انبار داده (فعلاً غیرقابل پرس‌وجو)</p>');
      out.push(`<p dir="ltr" class="admin-check-detail">${report.warehouse_only.map(escapeHtml).join(", ")}</p>`);
    }
    if (report.schema_only && report.schema_only.length) {
      out.push('<p class="admin-section-title">فقط در schema.yaml (در اجرا ناموفق خواهد شد)</p>');
      out.push(`<p dir="ltr" class="admin-check-detail">${report.schema_only.map(escapeHtml).join(", ")}</p>`);
    }
    if (report.type_changed && report.type_changed.length) {
      out.push('<p class="admin-section-title">نوع ستون تغییر کرده</p>');
      out.push(twoColumnTable(
        "ستون", "نوع قبلی → نوع فعلی",
        report.type_changed.map((c) => [c.column, `${c.previous_type} → ${c.current_type}`]),
      ));
    }
  }
  if (!report.baseline_available) {
    out.push('<p class="admin-loading">اولین اجرا — مبنایی برای مقایسهٔ نوع ستون هنوز ثبت نشده است.</p>');
  }
  body.innerHTML = out.join("");
}

/* ── Admin panel phase 6: vocabulary freshness + manual refresh ─────── */

function renderVocabulary(payload) {
  const body = $("vocabulary-body");
  const columns = payload.columns || [];

  const wrap = document.createElement("div");
  wrap.className = "admin-table-wrap";
  const table = document.createElement("table");
  table.className = "admin-table";
  table.innerHTML =
    "<thead><tr><th>ستون</th><th>وضعیت</th><th>تعداد مقدار</th>" +
    "<th>آخرین بروزرسانی</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");

  columns.forEach((c) => {
    const freshCls = c.cached ? (c.is_fresh ? "pass" : "skip") : "fail";
    const freshLabel = c.cached ? (c.is_fresh ? "تازه" : "کهنه") : "هرگز";
    const failureNote = c.last_failure
      ? `<span class="admin-status-pill admin-status-fail">آخرین تلاش ناموفق</span>`
      : "";

    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td dir="ltr">${escapeHtml(c.table)}.${escapeHtml(c.column)}</td>` +
      `<td><span class="admin-status-pill admin-status-${freshCls}">${escapeHtml(freshLabel)}</span> ${failureNote}</td>` +
      `<td class="num">${c.value_count === null ? "—" : escapeHtml(fmtNum(c.value_count))}</td>` +
      `<td dir="ltr">${escapeHtml(c.fetched_at || "—")}</td>`;

    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "admin-btn-refresh";
    btn.type = "button";
    btn.textContent = "بازخوانی";
    /* Same reasoning as renderKeys' data-pid/data-hash: table/column
     * reach the DOM as `dataset` assignments, never interpolated into a
     * quoted `data-vocab-refresh="..."` attribute string, so neither
     * value can break out of an attribute regardless of its contents. */
    btn.dataset.vocabRefresh = `${c.table}|${c.column}`;
    actionTd.appendChild(btn);
    tr.appendChild(actionTd);
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  wrap.appendChild(table);
  body.innerHTML = "";
  body.appendChild(wrap);

  body.querySelectorAll("[data-vocab-refresh]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [table, column] = btn.dataset.vocabRefresh.split("|");
      btn.disabled = true;
      try {
        const result = await api.vocabularyRefresh(table, column);
        if (!result.ok) {
          showNotice("error", `بازخوانی ${table}.${column} ناموفق بود: ${result.error}`);
        }
        await refreshOne("vocabulary");
      } catch (err) {
        showNotice("error", `بازخوانی ناموفق بود: ${err.message || err}`);
        btn.disabled = false;
      }
    });
  });
}

/* ── Admin panel phase 6: per-analyst usage and rate-limit pressure ──── */

function renderUsage(report) {
  const body = $("usage-body");
  const principals = report.principals || {};
  const out = [];
  if (report.rate_limit_never_triggered) {
    out.push(
      '<p class="admin-rail-summary"><strong>محدودیت نرخ تاکنون برای هیچ‌کس فعال نشده است</strong></p>',
    );
  }
  const entries = Object.values(principals);
  if (!entries.length) {
    out.push('<p class="admin-loading">داده‌ای در این بازه یافت نشد.</p>');
    body.innerHTML = out.join("");
    return;
  }
  const rows = entries.map((p) => {
    const lat = p.latency_ms || {};
    return `<tr>
      <td dir="ltr">${escapeHtml(p.principal_id)}</td>
      <td class="num">${escapeHtml(fmtNum(p.queries))}</td>
      <td class="num">${escapeHtml(fmtNum(p.failures))}</td>
      <td class="num" dir="ltr">${lat.p50 !== null && lat.p50 !== undefined ? Math.round(lat.p50) : "—"}</td>
      <td class="num" dir="ltr">${lat.p95 !== null && lat.p95 !== undefined ? Math.round(lat.p95) : "—"}</td>
      <td class="num">${escapeHtml(fmtNum(p.rate_limit_hits))}</td>
    </tr>`;
  });
  out.push(
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    "<th>تحلیل‌گر</th><th>پرس‌وجوها</th><th>ناموفق</th><th>p50 (ms)</th><th>p95 (ms)</th><th>برخورد با محدودیت</th>" +
    `</tr></thead><tbody>${rows.join("")}</tbody></table></div>`,
  );
  body.innerHTML = out.join("");
}

/* ── Admin panel phase 6 §9: failed-authentication visibility ───────── */

function renderAuthFailures(summary) {
  const body = $("authFailures-body");
  const tiles = [
    statTile({ label: "کل تلاش‌های ناموفق", value: fmtNum(summary.total ?? 0) }),
    statTile({ label: "از مسیرهای مدیریتی", value: fmtNum(summary.admin_path_total ?? 0) }),
  ];
  const bySource = summary.by_source_ip || {};
  const rows = Object.entries(bySource);
  body.innerHTML =
    `<div class="admin-stats">${tiles.join("")}</div>` +
    (rows.length ? twoColumnTable("آدرس مبدأ", "تعداد", rows) : "");
}

/* ── Admin panel phase 7: key lifecycle and roles ───────────────────
 *
 * The routes behind this card have existed since phase 2 with nothing in
 * the UI calling them, so issuing a key or granting a role meant the CLI
 * plus a server restart -- and a revocation that phase 2 made immediate
 * was, in practice, "immediate once someone reaches a terminal".
 *
 * Three things here are not visible from the routes alone, and this card
 * exists to surface all three at the moment each one matters:
 *
 * 1. A key issued through the API starts with EVERY column denied
 *    (appdb.key_store._maximally_restrictive_denied_columns). It
 *    authenticates and can read nothing. That is deliberate -- issuing is
 *    `operations`, and widening what a key can see is `security`'s -- but
 *    an operations admin who does not know it has just minted a key that
 *    looks broken to whoever they hand it to.
 * 2. Disable is reversible. Revoke is not. They are one button apart.
 * 3. The raw key exists exactly once, in the issue response. Nothing
 *    stores it -- not the server, not this page.
 */

/** The two independent reads this card needs. Role holders are fetched
 * alongside the key list because a role attaches to a principal id rather
 * than to a key, so a row cannot show its own roles without them. */
async function loadKeysCard() {
  const [keys, ops, sec] = await Promise.all([
    api.listKeys(),
    api.roleHolders("operations"),
    api.roleHolders("security"),
  ]);
  return {
    keys: keys.keys || [],
    operations: new Set(ops.principal_ids || []),
    security: new Set(sec.principal_ids || []),
  };
}

function keyState(row) {
  if (row.revoked_at) return { label: "ابطال‌شده", cls: "fail" };
  if (row.disabled_at) return { label: "غیرفعال", cls: "skip" };
  return { label: "فعال", cls: "pass" };
}

function renderKeys({ keys, operations, security }) {
  const body = $("keys-body");
  if (!keys.length) {
    body.innerHTML = '<p class="admin-loading">هیچ کلیدی ثبت نشده است.</p>';
    return;
  }

  const wrap = document.createElement("div");
  wrap.className = "admin-table-wrap";
  const table = document.createElement("table");
  table.className = "admin-table";
  table.innerHTML =
    "<thead><tr><th>شناسه</th><th>نام</th><th>وضعیت</th><th>منبع</th>" +
    "<th>ستون‌های ممنوع</th><th>اقدام</th></tr></thead>";
  const tbody = document.createElement("tbody");

  keys.forEach((row) => {
    const st = keyState(row);
    const denied = row.denied_columns || [];
    /* "No restriction" and "some columns" are different kinds of fact, and
     * a bare count makes the first one read as zero of something rather
     * than the absence of a restriction. */
    const deniedLabel = denied.length === 0 ? "بدون محدودیت" : `${fmtNum(denied.length)} ستون`;
    const roleTags =
      (operations.has(row.principal_id) ? '<span class="key-role">operations</span>' : "") +
      (security.has(row.principal_id) ? '<span class="key-role">security</span>' : "");
    const revoked = Boolean(row.revoked_at);
    const toggleAct = row.disabled_at ? "enable" : "disable";
    const toggleLabel = row.disabled_at ? "فعال کردن" : "غیرفعال کردن";

    const actions = revoked
      ? ""
      : `<button type="button" data-act="${toggleAct}">${toggleLabel}</button>` +
        '<button type="button" data-act="acl">ستون‌ها</button>' +
        '<button type="button" data-act="role">نقش‌ها</button>' +
        '<button type="button" data-act="revoke" class="key-danger">ابطال</button>';

    const tr = document.createElement("tr");
    /* Set through the element's own `dataset`, not interpolated into a
     * quoted `data-hash="..."` / `data-pid="..."` attribute string
     * (finding 2's actual sink). A value assigned this way is DOM text,
     * never re-parsed as markup, so a principal_id containing a `"`
     * cannot break out of an attribute -- there is no such attribute
     * string for it to break out of, whatever escapeHtml does or does
     * not encode. */
    tr.dataset.hash = row.key_sha256;
    tr.dataset.pid = row.principal_id;
    tr.innerHTML =
      `<td><code>${escapeHtml(row.principal_id)}</code>${roleTags}</td>` +
      `<td>${escapeHtml(row.name)}</td>` +
      `<td><span class="admin-status-pill admin-status-${st.cls}">${st.label}</span></td>` +
      `<td>${escapeHtml(row.source === "imported_from_env" ? ".env" : "پنل")}</td>` +
      `<td>${escapeHtml(deniedLabel)}</td>` +
      `<td class="key-actions">${actions}</td>`;
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  wrap.appendChild(table);

  body.innerHTML = "";
  body.appendChild(wrap);
  const note = document.createElement("p");
  note.className = "admin-loading";
  note.textContent =
    "«غیرفعال کردن» برگشت‌پذیر است؛ «ابطال» نیست — ردیف بایگانی می‌شود و هرگز " +
    "حذف نمی‌شود، تا بازگرداندن دیتابیس به دیروز کلیدی را که نشت کرده دوباره " +
    "زنده نکند.";
  body.appendChild(note);

  body.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => onKeyAction(btn));
  });
}

async function onKeyAction(btn) {
  const tr = btn.closest("tr");
  const hash = tr.dataset.hash;
  const pid = tr.dataset.pid;
  const act = btn.dataset.act;

  try {
    if (act === "disable" || act === "enable") {
      btn.disabled = true;
      await (act === "disable" ? api.disableKey(hash) : api.enableKey(hash));
    } else if (act === "revoke") {
      /* Typing the id, rather than confirming a yes/no: every row's
       * buttons sit in the same position, and this is the only one that
       * cannot be undone. A confirm dialog defends against not reading;
       * this defends against clicking the right button on the wrong row. */
      const typed = window.prompt(
        "ابطال دائمی و بازگشت‌ناپذیر است. برای تأیید، شناسهٔ کلید را تایپ کنید:\n" + pid,
      );
      if (typed !== pid) return;
      btn.disabled = true;
      await api.revokeKey(hash);
    } else if (act === "acl") {
      const answer = window.prompt(
        "ستون‌هایی که این کلید هرگز نباید ببیند، جدا شده با کاما.\n" +
        "خالی بگذارید تا هیچ محدودیتی نداشته باشد.",
        "",
      );
      if (answer === null) return;
      const columns = answer.split(",").map((c) => c.trim()).filter(Boolean);
      btn.disabled = true;
      await api.updateKeyAcl(hash, columns);
    } else if (act === "role") {
      const answer = window.prompt(
        "نقش‌های " + pid + "\n" +
        "برای اعطا: operations یا security\n" +
        "برای سلب: -operations یا -security",
      );
      if (!answer) return;
      const grant = !answer.trim().startsWith("-");
      const capability = grant ? answer.trim() : answer.trim().slice(1).trim();
      if (capability !== "operations" && capability !== "security") {
        showNotice("error", "نقش باید operations یا security باشد.");
        return;
      }
      btn.disabled = true;
      await api.changeRole(pid, capability, grant);
    }
    await refreshOne("keys");
  } catch (err) {
    /* 409 is the server refusing to remove the last holder of a role, and
     * 403 is this key lacking `security` for an ACL or role change. Both
     * are real answers rather than failures of this page, so the server's
     * own message is shown instead of a generic one. */
    showNotice("error", err.message || String(err));
    btn.disabled = false;
  }
}

/* The one-time reveal. Deliberately not written to localStorage, not put
 * in the URL, and never re-rendered by a later refresh: this is the only
 * moment the raw key exists anywhere outside this page's memory, and
 * every additional place it lands is a place it can be read from later.
 * It also lives outside `keys-body` so a background refresh of the list
 * cannot re-render it away while the key it shows is still the only copy
 * in existence. */
function revealIssuedKey(payload) {
  const el = $("keys-reveal");
  el.hidden = false;
  el.innerHTML =
    "<h3>کلید خام — همین حالا کپی کنید</h3>" +
    "<p>این مقدار فقط همین یک‌بار نمایش داده می‌شود و هیچ‌جا ذخیره نشده است. " +
    "اگر این صفحه را ببندید، بازیابی‌پذیر نیست.</p>" +
    `<code class="keys-reveal-value" dir="ltr">${escapeHtml(payload.raw_key)}</code>` +
    `<p>${fmtNum(payload.raw_key.length)} نویسه. این چیزی است که صاحب کلید در فیلد ` +
    "بالای صفحه می‌گذارد — در <code>.env</code> نمی‌رود.</p>" +
    '<p class="keys-reveal-warn">این کلید با <strong>همهٔ ستون‌ها ممنوع</strong> صادر ' +
    "شده است: احراز هویت می‌شود ولی تا وقتی یک ادمین امنیت با دکمهٔ «ستون‌ها» " +
    "محدودیتش را باز نکند، هیچ پرس‌وجویی برایش کار نمی‌کند.</p>" +
    '<button type="button" id="keys-reveal-dismiss">کپی کردم، ببند</button>';
  $("keys-reveal-dismiss").addEventListener("click", () => {
    el.hidden = true;
    el.innerHTML = "";
  });
}

$("keys-issue-btn").addEventListener("click", async () => {
  const principalId = window.prompt("شناسهٔ پایدار کلید (مثلاً analyst-2):");
  if (!principalId || !principalId.trim()) return;
  const name = window.prompt("نام قابل خواندن (برای لاگ و ممیزی):");
  if (!name || !name.trim()) return;

  const btn = $("keys-issue-btn");
  btn.disabled = true;
  try {
    revealIssuedKey(await api.issueKey(principalId.trim(), name.trim()));
    await refreshOne("keys");
  } catch (err) {
    showNotice("error", `صدور کلید ناموفق بود: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
});
