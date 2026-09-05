/* web/admin/main.js — admin panel bootstrap and rendering.
 *
 * A dashboard, not a tool (docs/admin-panel-architecture.md / the phase-1
 * spec): every card below only ever reads. There is no control anywhere
 * on this page that changes server state -- see web/admin/admin.js's
 * module docstring for why that is a hard line, not a style choice.
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
 * one endpoint does not block the rest. */
const CARDS = ["summary", "health", "cache", "config"];

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

function renderCache(stats) {
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
