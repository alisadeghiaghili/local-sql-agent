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
  "summary", "health", "cache", "config", "feedback",
  "maintenance", "schemaDrift", "vocabulary", "usage", "authFailures",
];

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

function updateKeyStatus() {
  const el = $("live-key-status");
  if (hasApiKey()) {
    el.textContent = "کلید: ذخیره شده ✓";
    el.className = "live-key-status set";
  } else {
    el.textContent = "کلید: تنظیم نشده";
    el.className = "live-key-status unset";
  }
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
}

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
      renderHealth(await api.healthChecks());
    } else if (name === "cache") {
      renderCache(await api.cache());
    } else if (name === "config") {
      renderConfig(await api.config());
    } else if (name === "feedback") {
      const status = $("feedback-status-filter").value;
      const [stats, list] = await Promise.all([api.feedbackStats(), api.feedbackList(status)]);
      renderFeedback(stats, list.feedback || []);
    } else if (name === "maintenance") {
      renderMaintenance(await api.maintenanceState());
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
    if (err instanceof AdminForbiddenError) {
      showForbiddenBanner(
        "این کلید، کلید مدیریتی نیست — این کلید معتبر است ولی دسترسی مدیریتی ندارد. " +
        (err.message || ""),
      );
    } else if (err instanceof AdminUnauthorizedError) {
      showNotice("error", "کلید API وارد نشده یا نامعتبر است. یک کلید مدیریتی وارد کنید.");
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
  const div = document.createElement("div");
  div.textContent = String(s);
  return div.innerHTML;
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
  const rows = columns.map((c) => {
    const freshCls = c.cached ? (c.is_fresh ? "pass" : "skip") : "fail";
    const freshLabel = c.cached ? (c.is_fresh ? "تازه" : "کهنه") : "هرگز";
    const failureNote = c.last_failure
      ? `<span class="admin-status-pill admin-status-fail">آخرین تلاش ناموفق</span>`
      : "";
    return `<tr>
      <td dir="ltr">${escapeHtml(c.table)}.${escapeHtml(c.column)}</td>
      <td><span class="admin-status-pill admin-status-${freshCls}">${escapeHtml(freshLabel)}</span> ${failureNote}</td>
      <td class="num">${c.value_count === null ? "—" : escapeHtml(fmtNum(c.value_count))}</td>
      <td dir="ltr">${escapeHtml(c.fetched_at || "—")}</td>
      <td><button class="admin-btn-refresh" data-vocab-refresh="${escapeHtml(c.table)}|${escapeHtml(c.column)}" type="button">بازخوانی</button></td>
    </tr>`;
  });
  body.innerHTML =
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    "<th>ستون</th><th>وضعیت</th><th>تعداد مقدار</th><th>آخرین بروزرسانی</th><th></th>" +
    `</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;

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
