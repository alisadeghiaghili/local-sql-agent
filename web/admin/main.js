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

function renderSummary(report) {
  const body = $("summary-body");
  const modeClass = report.mode === "aggregate_with_examples" ? "examples" : "";
  const rows = [];
  rows.push(`<span class="admin-mode-pill ${modeClass}">${escapeHtml(report.mode)}</span>`);
  rows.push(`<dl class="admin-kv">`);
  rows.push(kv("تعداد رکوردها", report.record_count));
  rows.push(kv("بازهٔ زمانی", `${report.time_range?.start ?? "—"} .. ${report.time_range?.end ?? "—"}`));
  const lat = report.latency?.overall_ms;
  if (lat && lat.count) {
    rows.push(kv("تأخیر کلی (p50/p95/p99 ms)", `${lat.p50?.toFixed?.(0) ?? lat.p50} / ${lat.p95?.toFixed?.(0) ?? lat.p95} / ${lat.p99?.toFixed?.(0) ?? lat.p99}`));
  }
  const ft = report.failure_taxonomy;
  if (ft) {
    rows.push(kv("موفق / ناموفق", `${ft.success_count} / ${ft.failure_count}`));
  }
  const cb = report.cache_behaviour;
  if (cb) {
    rows.push(kv("نرخ کش T0", cb.t0_rate ?? "—"));
    rows.push(kv("نرخ prefix cache hit", cb.prefix_cache_hit_rate ?? "—"));
  }
  rows.push(`</dl>`);

  if (report.records_by_model && Object.keys(report.records_by_model).length) {
    rows.push('<p class="admin-section-title">رکورد به تفکیک مدل</p>');
    rows.push('<div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>مدل</th><th>تعداد</th></tr></thead><tbody>');
    for (const [model, count] of Object.entries(report.records_by_model)) {
      rows.push(`<tr><td dir="ltr">${escapeHtml(model)}</td><td>${count}</td></tr>`);
    }
    rows.push("</tbody></table></div>");
  }

  body.innerHTML = rows.join("");
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
  const rows = checks.map((c) => {
    const cls = c.status === "PASS" ? "pass" : c.status === "FAIL" ? "fail" : "skip";
    return `<tr>
      <td>${escapeHtml(c.name)}</td>
      <td><span class="admin-status-pill admin-status-${cls}">${escapeHtml(c.status)}</span></td>
      <td>${escapeHtml(c.detail || "")}</td>
    </tr>`;
  });
  body.innerHTML = (
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    "<th>بررسی</th><th>وضعیت</th><th>توضیح</th></tr></thead><tbody>" +
    rows.join("") +
    "</tbody></table></div>"
  );
}

function renderCache(stats) {
  const body = $("cache-body");
  const rows = [
    kv("فعال", stats.enabled),
    kv("اندازهٔ فعلی", stats.size),
    kv("hits", stats.hits),
    kv("misses", stats.misses),
    kv("evictions", stats.evictions),
  ];
  if (stats.sql_index_size !== undefined) rows.push(kv("اندازهٔ نمایهٔ SQL", stats.sql_index_size));
  if (stats.max_size !== undefined) rows.push(kv("حداکثر اندازه", stats.max_size));
  if (stats.ttl_seconds !== undefined) rows.push(kv("TTL (ثانیه)", stats.ttl_seconds));
  body.innerHTML = `<dl class="admin-kv">${rows.join("")}</dl>`;
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
      <td>${f.count ?? "—"}</td>
      <td>${escapeHtml(f.error || "")}</td>
    </tr>`;
  });
  body.innerHTML = (
    `<p class="admin-loading" dir="ltr">project_config_dir: ${escapeHtml(payload.project_config_dir || "")}</p>` +
    '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
    "<th>فایل</th><th>وضعیت</th><th>تعداد ورودی</th><th>خطا</th></tr></thead><tbody>" +
    rows.join("") +
    "</tbody></table></div>"
  );
}
