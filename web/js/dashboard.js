/* web/js/dashboard.js — the dashboard surfaces around the conversation.
 *
 * The engine, the session index and the transcript all live in main.js and
 * render/*; this module only READS what they already resolved and paints
 * the four dashboard panels plus the shell's own affordances (rail, search,
 * status popover, conversation drawer). It never invents a number: every
 * figure is derived from the session index (`turn_count`, timestamps), the
 * active transcript's turns (`timings`, `result`, `guard`, `error`) or the
 * live `/health` lights that setHealth() already renders.
 *
 * Data flows one way: main.js calls syncDashboard({sessions, turns, …})
 * whenever the index or the transcript changes; this module re-renders.
 */

"use strict";

import { t } from "./i18n.js";
import { fmt as faNum, technical } from "./num.js";

const $ = (id) => document.getElementById(id);

/** Kept in sync by syncDashboard; the panels re-render from it. */
let view = { sessions: [], turns: [], mode: null, baseUrl: "", sessionId: null };
let rangeDays = 7;

/* ── Small helpers ──────────────────────────────────────────────────── */

function num(value) {
  const latin = document.documentElement.lang === "en";
  return (latin ? technical : faNum)(value);
}

function dayKey(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

function dayLabel(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(document.documentElement.lang === "en" ? "en-GB" : "fa-IR", {
    month: "short",
    day: "numeric",
  });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Sum of a numeric field across turns, ignoring missing values. */
function sumBy(items, pick) {
  return items.reduce((acc, item) => {
    const v = pick(item);
    return Number.isFinite(v) ? acc + v : acc;
  }, 0);
}

/** One turn's own status, as the three-state dot the cards use. */
export function turnStatus(turn) {
  if (!turn) return "unknown";
  if (turn.error) return "bad";
  if (turn.guard && turn.guard.verdict === "rejected") return "bad";
  if (!turn.result && !turn.sql) return "running";
  if (turn.result && turn.result.row_count === 0) return "warn";
  return "ok";
}

function turnLatency(turn) {
  const timings = turn && turn.timings ? turn.timings : {};
  const llm = turn && turn.llm ? turn.llm : {};
  for (const value of [timings.total_ms, llm.total_ms]) {
    if (Number.isFinite(value)) return value;
  }
  return null;
}

/* ── Range filtering ────────────────────────────────────────────────── */

/** Sessions whose last activity falls inside the selected window. */
function sessionsInRange() {
  if (!rangeDays) return view.sessions.slice();
  const cutoff = Date.now() - rangeDays * 24 * 3600 * 1000;
  return view.sessions.filter((s) => {
    const at = new Date(s.last_active_at || s.created_at || 0).getTime();
    return Number.isFinite(at) && at >= cutoff;
  });
}

/* ── KPIs ───────────────────────────────────────────────────────────── */

/** Health facts are already on screen in #health; read them from there
 * rather than probing the API a second time from this module. */
function healthFacts() {
  const host = $("health");
  const pills = host ? Array.from(host.querySelectorAll(".pill")) : [];
  const facts = pills.map((pill) => {
    const dot = pill.querySelector(".dot");
    const state = dot ? (dot.className.match(/dot-(ok|down|unknown)/) || [])[1] : "unknown";
    return {
      name: (pill.querySelector(".pill-name") || {}).textContent || "",
      state: state || "unknown",
      label: (pill.querySelector(".pill-state") || {}).textContent || "",
    };
  });
  const up = facts.filter((f) => f.state === "ok").length;
  return { facts, up, total: facts.length };
}

function renderKpis() {
  const sessions = sessionsInRange();
  const turns = view.turns || [];

  const queries = sumBy(sessions, (s) => Number(s.turn_count) || 0);
  $("kpi-queries").textContent = sessions.length ? num(queries) : "—";

  // Success is measured on the transcript that is actually loaded: a guard
  // rejection and a transport/execution error both count as not successful,
  // and a turn still running is not counted at all.
  const settled = turns.filter((turn) => turnStatus(turn) === "ok" || turnStatus(turn) === "warn" || turnStatus(turn) === "bad");
  const ok = settled.filter((turn) => turnStatus(turn) !== "bad").length;
  $("kpi-success").textContent = settled.length ? `${num(Math.round((100 * ok) / settled.length))}%` : "—";

  const latencies = turns.map(turnLatency).filter((v) => Number.isFinite(v));
  $("kpi-latency").textContent = latencies.length
    ? `${num(Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length))} ms`
    : "—";

  const rows = sumBy(turns, (turn) => (turn.result ? Number(turn.result.row_count) : NaN));
  $("kpi-rows").textContent = turns.some((turn) => turn.result) ? num(rows) : "—";

  const health = healthFacts();
  const conn = $("kpi-conn");
  const dot = el("span", `dot dot-${health.total ? (health.up === health.total ? "ok" : health.up ? "unknown" : "down") : "unknown"}`);
  conn.replaceChildren(dot, document.createTextNode(health.total ? `${num(health.up)}/${num(health.total)}` : "—"));
}

/* ── Chart ──────────────────────────────────────────────────────────── */

let chartActive = -1;
let chartPoints = [];

function chartSeries() {
  // One point per conversation, newest last: x = when it was last used,
  // y = how many questions it holds. Both come straight from the index.
  return sessionsInRange()
    .slice()
    .sort((a, b) => new Date(a.last_active_at || a.created_at) - new Date(b.last_active_at || b.created_at))
    .map((s) => ({
      x: new Date(s.last_active_at || s.created_at).getTime(),
      y: Number(s.turn_count) || 0,
      label: dayLabel(s.last_active_at || s.created_at),
      title: s.title || t("untitledSession"),
      id: s.session_id,
    }));
}

function smoothPath(pts) {
  if (pts.length < 2) return "";
  let d = `M${pts[0].x},${pts[0].y}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] || p2;
    const t1x = p1.x + (p2.x - p0.x) / 6;
    const t1y = p1.y + (p2.y - p0.y) / 6;
    const t2x = p2.x - (p3.x - p1.x) / 6;
    const t2y = p2.y - (p3.y - p1.y) / 6;
    d += ` C${t1x},${t1y} ${t2x},${t2y} ${p2.x},${p2.y}`;
  }
  return d;
}

function svgNode(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function renderChart() {
  const host = $("chart-host");
  if (!host) return;
  const series = chartSeries();
  const total = series.reduce((a, p) => a + p.y, 0);
  $("chart-total").textContent = num(total);

  const w = Math.max(280, host.clientWidth || 560);
  const h = Math.max(160, host.clientHeight || 240);
  // The side gutters are wider than the axis labels strictly need: the
  // tooltip is centred on its point and is up to 230px wide, so a plot that
  // ran edge to edge put the first and last readouts half outside the panel.
  const pad = { top: 30, right: 34, bottom: 24, left: 40 };
  const innerW = w - pad.left - pad.right;
  const innerH = h - pad.top - pad.bottom;

  const svg = svgNode("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, "aria-hidden": "true" });

  // Gradient fill + soft glow, defined per render so theme tokens apply.
  const defs = svgNode("defs", {});
  const grad = svgNode("linearGradient", { id: "chart-area", x1: "0", y1: "0", x2: "0", y2: "1" });
  grad.appendChild(svgNode("stop", { offset: "0%", class: "chart-area-top" }));
  grad.appendChild(svgNode("stop", { offset: "100%", class: "chart-area-bottom" }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  const maxY = Math.max(1, ...series.map((p) => p.y));
  const yFor = (v) => pad.top + innerH - (v / maxY) * innerH;

  // Grid: three hairlines, labelled in the reading direction's numerals.
  [0, 0.5, 1].forEach((frac) => {
    const y = pad.top + innerH - frac * innerH;
    svg.appendChild(svgNode("line", {
      x1: pad.left, x2: w - pad.right, y1: y, y2: y, class: "chart-grid-line",
    }));
    const label = svgNode("text", {
      x: pad.left - 10, y: y + 3, class: "chart-axis-label", "text-anchor": "end",
    });
    label.textContent = num(Math.round(maxY * frac));
    svg.appendChild(label);
  });

  if (!series.length) {
    svg.appendChild(svgNode("line", {
      x1: pad.left, x2: w - pad.right,
      y1: pad.top + innerH - 2, y2: pad.top + innerH - 2,
      class: "chart-empty-line",
    }));
    const empty = svgNode("text", {
      x: pad.left + innerW / 2, y: pad.top + innerH / 2, class: "chart-empty-text", "text-anchor": "middle",
    });
    empty.textContent = t("chartEmpty");
    svg.appendChild(empty);
    host.replaceChildren(svg);
    chartPoints = [];
    hideTip();
    $("chart-foot").textContent = t("chartEmptyHint");
    return;
  }

  $("chart-foot").textContent = t("chartFoot");

  const step = series.length > 1 ? innerW / (series.length - 1) : 0;
  // `value` is the datum; `y` is where it is drawn. Collapsing both into
  // one field is what made the tooltip quote a pixel offset as a count.
  const pts = series.map((p, i) => ({
    ...p,
    value: p.y,
    x: pad.left + (series.length > 1 ? i * step : innerW / 2),
    y: yFor(p.y),
  }));

  const linePath = series.length > 1
    ? smoothPath(pts)
    : `M${pts[0].x},${pts[0].y} L${pts[0].x + 0.01},${pts[0].y}`;

  const areaPath = `${linePath} L${pts[pts.length - 1].x},${pad.top + innerH} L${pts[0].x},${pad.top + innerH} Z`;
  svg.appendChild(svgNode("path", { d: areaPath, fill: "url(#chart-area)", stroke: "none" }));
  svg.appendChild(svgNode("path", { d: linePath, class: "chart-line" }));

  // X labels: first, middle and last, so the axis stays readable.
  const labelIdx = series.length <= 3
    ? series.map((_, i) => i)
    : [0, Math.floor((series.length - 1) / 2), series.length - 1];
  labelIdx.forEach((i) => {
    const label = svgNode("text", {
      x: pts[i].x, y: h - 6, class: "chart-axis-label",
      "text-anchor": i === 0 ? "start" : i === series.length - 1 ? "end" : "middle",
    });
    label.textContent = pts[i].label;
    svg.appendChild(label);
  });

  pts.forEach((p, i) => {
    svg.appendChild(svgNode("circle", { cx: p.x, cy: p.y, r: 9, class: "chart-point-halo", "data-i": i }));
    svg.appendChild(svgNode("circle", { cx: p.x, cy: p.y, r: 4, class: "chart-point" }));
    const hit = svgNode("rect", {
      x: p.x - Math.max(12, step / 2 || 20), y: pad.top - 6,
      width: Math.max(24, step || 40), height: innerH + 12,
      fill: "transparent", "data-hit": i,
    });
    hit.addEventListener("pointerenter", () => showTip(i));
    hit.addEventListener("pointermove", () => showTip(i));
    hit.addEventListener("pointerleave", hideTip);
    svg.appendChild(hit);
  });

  host.replaceChildren(svg);
  chartPoints = pts;
  // The brightest point carries the reference's floating readout: the
  // highest-volume conversation, unless the analyst is hovering another.
  const peak = pts.reduce((best, p, i) => (p.value > pts[best].value ? i : best), 0);
  chartActive = peak;
  showTip(peak);
}

function showTip(index) {
  const host = $("chart-host");
  if (!host || !chartPoints[index]) return;
  chartActive = index;
  const p = chartPoints[index];
  let tip = host.querySelector(".chart-tip");
  if (!tip) {
    tip = el("div", "chart-tip");
    host.appendChild(tip);
  }
  tip.replaceChildren(
    el("span", "chart-tip-value", `${num(p.value)} ${t("chartTipUnit")}`),
    el("span", "chart-tip-label", `${p.label} · ${p.title}`),
  );
  const hostRect = host.getBoundingClientRect();
  const svg = host.querySelector("svg");
  const scale = svg ? svg.getBoundingClientRect().width / (svg.viewBox.baseVal.width || 1) : 1;
  tip.hidden = false;
  // Keep the readout inside the panel: it is centred on its point, so at
  // the first and last point half of it used to sit outside the chart.
  const half = tip.offsetWidth / 2;
  const left = Math.min(
    Math.max(p.x * scale, half + 2),
    Math.max(half + 2, hostRect.width - half - 2),
  );
  tip.style.left = `${left}px`;
  tip.style.top = `${p.y * scale}px`;
}

function hideTip() {
  const tip = $("chart-host")?.querySelector(".chart-tip");
  if (tip) tip.hidden = true;
}

/* ── Recent queries ─────────────────────────────────────────────────── */

function renderRecent() {
  const list = $("recent-list");
  const empty = $("recent-empty");
  if (!list) return;
  const turns = (view.turns || []).slice().reverse().slice(0, 5);
  list.replaceChildren();
  empty.hidden = turns.length > 0;

  for (const turn of turns) {
    const status = turnStatus(turn);
    const li = el("li");
    const btn = el("button", "recent-row");
    btn.type = "button";
    btn.appendChild(el("span", `recent-dot ${status}`));

    const q = el("span", "recent-q", turn.question || t("untitledTurn"));
    q.title = turn.question || "";
    btn.appendChild(q);

    const meta = el("span", "recent-meta");
    const latency = turnLatency(turn);
    if (turn.result) meta.appendChild(el("span", null, `${num(turn.result.row_count)} ${t("rowsUnit")}`));
    else if (turn.error || (turn.guard && turn.guard.verdict === "rejected")) meta.appendChild(el("span", null, t("failedTag")));
    if (Number.isFinite(latency)) meta.appendChild(el("span", null, `${num(Math.round(latency))} ms`));
    btn.appendChild(meta);

    btn.addEventListener("click", () => {
      const target = document.getElementById(`turn-${turn.turn_id}`);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

/* ── Connection activity & status popover ───────────────────────────── */

const SERVICE_KEYS = [
  ["API", "serviceApi"],
  ["LLM", "serviceLlm"],
  ["DB", "serviceDb"],
];

function stateLabel(state) {
  if (state === "ok") return t("healthStateUp");
  if (state === "down") return t("healthStateDown");
  return t("healthStateUnknown");
}

function hostLabel(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || "—";
  }
}

function renderStatusPanel() {
  const list = $("status-list");
  if (!list) return;
  const health = healthFacts();
  list.replaceChildren();
  [...health.facts].forEach((fact) => {
    const li = el("li", "status-item");
    li.appendChild(el("span", "status-item-name", fact.name));
    li.appendChild(el("span", "status-item-value", fact.label || stateLabel(fact.state)));
    list.appendChild(li);
  });
  const turns = (view.turns || []).length;
  const li = el("li", "status-item");
  li.appendChild(el("span", "status-item-name", t("conversationTurns")));
  li.appendChild(el("span", "status-item-value", num(turns)));
  list.appendChild(li);
}

/* ── Public: paint everything from the app's current state ──────────── */

export function syncDashboard(next = {}) {
  view = { ...view, ...next };
  if (!document.getElementById("kpi-queries")) return;
  renderKpis();
  renderChart();
  renderRecent();
  renderStatusPanel();
}

/* ── Shell wiring (independent of app state) ────────────────────────── */

function setDrawer(open) {
  const body = document.querySelector(".app-body");
  const toggle = $("sidebar-toggle");
  const scrim = $("sidebar-scrim");
  if (!body) return;
  body.classList.toggle("sidebar-open", open);
  if (toggle) toggle.setAttribute("aria-expanded", String(open));
  if (scrim) scrim.hidden = !open;
  if (open) {
    const row = document.querySelector(".session-row.active .session-row-main");
    if (row && typeof row.focus === "function") row.focus({ preventScroll: true });
  }
}

function toggleStatusPanel(force) {
  const panel = $("status-panel");
  const btn = $("status-btn");
  if (!panel) return;
  const open = force !== undefined ? force : panel.hidden;
  panel.hidden = !open;
  if (btn) btn.setAttribute("aria-expanded", String(open));
  if (open) renderStatusPanel();
}

function scrollTo(id) {
  const target = $(id);
  if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function focusComposer() {
  const q = $("question");
  if (!q) return;
  q.focus({ preventScroll: false });
  q.scrollIntoView({ behavior: "smooth", block: "center" });
}

/** Filters `#session-list-host .session-row` by substring match on its
 * text content. Returns true when a query is active. Exported because
 * main.js's renderSessionSidebar() rebuilds the list wholesale — without
 * re-applying the filter, a search that was active the moment before would
 * silently clear (the row hidden states are DOM-attached, not data). */
export function filterSessions(raw) {
  let query = (raw || "").trim().toLowerCase();
  // Don't filter on a single keystroke — a one-character query hides most
  // rows before the user has committed to a term, and the list flickers on
  // every typo and correction. Two characters is the standard
  // search-as-you-type minimum: still responsive, but the query is a
  // plausible fragment of a real word rather than a guess.
  if (query.length < 2) query = "";
  const rows = Array.from(document.querySelectorAll("#session-list-host .session-row"));
  let visible = 0;
  rows.forEach((row) => {
    const hay = `${row.textContent || ""}`.toLowerCase();
    const match = !query || hay.includes(query);
    row.hidden = !match;
    if (match) visible += 1;
  });

  let note = $("search-empty-note");
  if (query && !visible) {
    if (!note) {
      note = el("p", "empty-line", t("searchEmpty"));
      note.id = "search-empty-note";
      note.style.padding = "0 16px 12px";
      $("session-list-host")?.appendChild(note);
    }
    note.hidden = false;
  } else if (note) {
    note.hidden = true;
  }
  return query.length > 0;
}

/** Wires the shell's own affordances. Called once from main.js. */
export function initDashboard() {
  const railMap = {
    dashboard: () => { setDrawer(false); window.scrollTo({ top: 0, behavior: "smooth" }); },
    // #analytics is a <details> that opens collapsed, so expanding it is
    // part of "go to analytics", not an extra step for the analyst.
    analytics: () => { const p = $("analytics"); if (p) p.open = true; scrollTo("analytics"); },
  };

  document.querySelectorAll("[data-rail]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const action = railMap[btn.dataset.rail];
      if (action) action();
      document.querySelectorAll("[data-rail]").forEach((other) => {
        other.classList.toggle("is-active", other === btn);
      });
    });
  });

  // "New query" opens a fresh conversation — the same action the drawer's
  // own button performs — and puts the caret in the composer.
  const railNewQuery = $("rail-new-query");
  if (railNewQuery) railNewQuery.addEventListener("click", () => {
    const fresh = $("btn-new-session");
    if (fresh) fresh.click();
    setTimeout(focusComposer, 60);
  });

  const statusBtn = $("status-btn");
  if (statusBtn) statusBtn.addEventListener("click", () => toggleStatusPanel());
  const statusClose = $("status-panel-close");
  if (statusClose) statusClose.addEventListener("click", () => toggleStatusPanel(false));

  const scrim = $("sidebar-scrim");
  if (scrim) scrim.addEventListener("click", () => setDrawer(false));

  // Choosing a conversation closes the drawer again.
  $("session-list-host")?.addEventListener("click", (event) => {
    if (event.target.closest(".session-row-main")) setDrawer(false);
  });

  const viewAll = $("recent-view-all");
  // The button lives inside the recent panel's <summary>, which itself toggles
  // the <details>. Stop propagation + preventDefault so "View all" only opens
  // the session drawer instead of also collapsing/expanding the panel.
  if (viewAll) viewAll.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setDrawer(true);
  });

  // The rail's conversation button IS main.js's #sidebar-toggle, which
  // owns the class toggling; mirror its state onto the scrim instead of
  // binding the same button twice.
  const body = document.querySelector(".app-body");
  if (body && typeof MutationObserver === "function") {
    new MutationObserver(() => {
      const open = body.classList.contains("sidebar-open");
      const scrimEl = $("sidebar-scrim");
      if (scrimEl) scrimEl.hidden = !open;
      $("sidebar-toggle")?.setAttribute("aria-expanded", String(open));
    }).observe(body, { attributes: true, attributeFilter: ["class"] });
  }

  // Search filters the conversation index and opens it while typing.
  const search = $("session-search");
  if (search) search.addEventListener("input", () => {
    const typing = filterSessions(search.value);
    if (typing) setDrawer(true);
  });

  document.querySelectorAll("#range-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      rangeDays = Number(btn.dataset.range) || 0;
      document.querySelectorAll("#range-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
      renderKpis();
      renderChart();
    });
  });

  document.addEventListener("keydown", (event) => {
    const tag = (event.target && event.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || event.target?.isContentEditable;
    if (event.key === "/" && !typing) {
      event.preventDefault();
      search?.focus();
    }
    if (event.key === "Escape") {
      toggleStatusPanel(false);
      const body = document.querySelector(".app-body");
      if (body?.classList.contains("sidebar-open")) setDrawer(false);
    }
  });

  // Re-measure the chart when the column changes width.
  if (typeof ResizeObserver === "function" && $("chart-host")) {
    let frame = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => renderChart());
    });
    ro.observe($("chart-host"));
  }

  // Health changes re-render the two cards that quote it.
  const health = $("health");
  if (health && typeof MutationObserver === "function") {
    new MutationObserver(() => {
      renderKpis();
      renderStatusPanel();
      syncAssistantState();
    }).observe(health, { childList: true, subtree: true });
  }
}
