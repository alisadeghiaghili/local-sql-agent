/* web/js/main.js — application bootstrap and the ask/render flow.
 *
 * SIMULATED mode replays SCENARIO from data.js turn by turn (sample buttons
 * drive the intended story; free-typed text is matched best-effort against
 * the same scripted turns). LIVE mode talks to the real backend per
 * docs/api-contract-v2.md — since /v2/* does not exist on the backend yet
 * (only /query and /health — see api/models.py), it must degrade honestly:
 * a 404 on /v2/sessions is shown as "this backend doesn't support v2 yet",
 * never papered over with a fabricated result.
 */

"use strict";

import { SCENARIO, SCENARIO_MATCH_HINTS } from "./data.js";
import { state, loadPersisted, persistTheme, persistBaseUrl, applyTheme, addTurn, findTurn } from "./state.js";
import { Api, V2NotSupportedError, ApiError, UnauthorizedError, RateLimitError } from "./api.js";
import { setApiKey, clearApiKey, hasApiKey } from "./apikey.js";
import { createTurnCard } from "./render/turn.js";
import { runSimulatedStages } from "./render/pipeline.js";

const $ = (id) => document.getElementById(id);

/* ── Boot ──────────────────────────────────────────────────────────── */
const params = new URLSearchParams(location.search);
loadPersisted();
if (params.get("live") === "1") state.mode = "live";
if (params.get("base")) state.baseUrl = params.get("base");
applyTheme();

let api = new Api(state.baseUrl);

renderSamples();
wireComposer();
wireTopbar();
setMode(state.mode, { skipHealthPrompt: true });
tickClock();
setInterval(tickClock, 1000);

/* ── Theme ─────────────────────────────────────────────────────────── */
function wireTopbar() {
  $("theme-toggle").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    const next = order[(order.indexOf(state.theme) + 1) % order.length];
    persistTheme(next);
    applyTheme();
    updateThemeLabel();
  });
  updateThemeLabel();

  $("mode-simulated").addEventListener("click", () => setMode("simulated"));
  $("mode-live").addEventListener("click", () => setMode("live"));

  $("live-base-connect").addEventListener("click", () => {
    const val = $("live-base-input").value.trim();
    if (!val) return;
    state.baseUrl = val.replace(/\/+$/, "");
    persistBaseUrl(state.baseUrl);
    api = new Api(state.baseUrl);
    refreshHealth();
  });

  $("live-key-save").addEventListener("click", () => {
    const val = $("live-key-input").value;
    if (!val.trim()) return;
    setApiKey(val);
    $("live-key-input").value = "";
    updateKeyStatus();
    showNotice("ok", "کلید API ذخیره شد — این کلید فقط در همین مرورگر نگه‌داری می‌شود.");
    refreshHealth();
  });

  $("live-key-clear").addEventListener("click", () => {
    clearApiKey();
    $("live-key-input").value = "";
    updateKeyStatus();
    showNotice("warn", "کلید API حذف شد. برای پرسیدن سؤال در حالت زندهٔ API باید دوباره یک کلید وارد کنید.");
  });
  updateKeyStatus();
}

/* ── API key status pill (topbar) ─────────────────────────────────── */
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

/** Reveal the key row, focus its input, and explain why — used both on
 * first live use and after a 401 (see handleLiveError). Never puts the
 * key itself, or any prior value, into the input or this message. */
function promptForApiKey(message) {
  $("live-key-row").hidden = false;
  showNotice("warn", message);
  $("live-key-input").focus();
}

function updateThemeLabel() {
  const labels = { system: "پوسته: سیستم", light: "پوسته: روشن", dark: "پوسته: تیره" };
  $("theme-toggle-label").textContent = labels[state.theme];
}

/* ── Mode switch ───────────────────────────────────────────────────── */
function setMode(mode, opts = {}) {
  state.mode = mode;
  $("mode-simulated").classList.toggle("active", mode === "simulated");
  $("mode-live").classList.toggle("active", mode === "live");
  $("live-base-row").hidden = mode !== "live";
  $("live-base-input").value = state.baseUrl;
  $("live-key-row").hidden = mode !== "live";
  updateKeyStatus();

  const foot = $("foot-mode");
  if (mode === "simulated") {
    foot.textContent = "حالت نمایشی — داده‌ها از پیش تعریف‌شده و کاملاً مصنوعی‌اند؛ هیچ پرس‌وجوی واقعی اجرا نشده است.";
    setHealth(true, true, true, "شبیه‌سازی‌شده — بدون اتصال واقعی");
  } else {
    foot.textContent = `حالت زندهٔ API — بک‌اند: ${state.baseUrl}`;
    if (!opts.skipHealthPrompt) refreshHealth();
    else setHealth(null, null, null, "در حال بررسی...");
  }
}

function setHealth(api_, llm, db, label) {
  const dot = (ok) => (ok === null ? "unknown" : ok ? "ok" : "down");
  $("health").innerHTML = [
    ["API", api_], ["LLM", llm], ["DB", db],
  ].map(([name, ok]) => `<span class="pill"><span class="dot dot-${dot(ok)}"></span>${name}</span>`).join("");
  $("health").title = label;
}

async function refreshHealth() {
  try {
    const h = await api.health();
    setHealth(h.api, h.llm, h.db, `/health: ${h.status}, model ${h.model || "?"}`);
  } catch {
    setHealth(false, false, false, "بک‌اند در دسترس نیست — uvicorn api.server:app را اجرا کنید یا حالت نمایشی را انتخاب کنید");
  }
}

/* ── Composer ──────────────────────────────────────────────────────── */
function renderSamples() {
  const wrap = $("samples");
  for (const turn of SCENARIO.turns) {
    const btn = document.createElement("button");
    btn.className = "sample";
    btn.type = "button";
    btn.textContent = turn.question;
    btn.dataset.turnId = turn.turn_id;
    btn.dir = "auto";
    btn.addEventListener("click", () => {
      if (state.busy) return;
      $("question").value = turn.question;
      ask();
    });
    wrap.appendChild(btn);
  }
}

function markSampleUsed(turnId) {
  const btn = document.querySelector(`.sample[data-turn-id="${turnId}"]`);
  if (btn) btn.disabled = true;
}

function wireComposer() {
  $("btn-ask").addEventListener("click", ask);
  $("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask();
    }
  });
}

function showNotice(kind, text) {
  const area = $("notice-area");
  const div = document.createElement("div");
  div.className = `notice ${kind}`;
  div.setAttribute("role", kind === "error" ? "alert" : "status");
  div.textContent = text;
  area.innerHTML = "";
  area.appendChild(div);
}

function clearNotice() {
  $("notice-area").innerHTML = "";
}

/* ── Ask flow ──────────────────────────────────────────────────────── */
async function ask() {
  const q = $("question").value.trim();
  if (!q || state.busy) return;
  state.busy = true;
  $("btn-ask").disabled = true;
  clearNotice();

  try {
    if (state.mode === "live") await askLive(q);
    else await askSimulated(q);
  } finally {
    state.busy = false;
    $("btn-ask").disabled = false;
    $("question").value = "";
  }
}

/* ── Simulated ─────────────────────────────────────────────────────── */
function matchScriptedTurn(q) {
  const exact = SCENARIO.turns.find((t) => t.question === q);
  if (exact) return exact;
  const norm = q.trim();
  let best = null, bestScore = 0;
  for (const t of SCENARIO.turns) {
    const hints = SCENARIO_MATCH_HINTS[t.turn_id] || [];
    let score = 0;
    for (const h of hints) if (norm.includes(h)) score++;
    if (score > bestScore) { bestScore = score; best = t; }
  }
  return bestScore > 0 ? best : null;
}

async function askSimulated(q) {
  const scripted = matchScriptedTurn(q);
  if (!scripted) {
    showNotice("info", "این پرسش در نسخهٔ نمایشی از پیش تعریف نشده است — یکی از نمونه‌های «داستان نمونه» را امتحان کنید.");
    return;
  }
  if (findTurn(scripted.turn_id)) {
    showNotice("info", "این پرسش پیش‌تر در همین گفتگو پاسخ داده شده — به پایین اسکرول کنید.");
    scrollToTurn(scripted.turn_id);
    return;
  }
  await appendTurnWithAnimation(scripted);
  markSampleUsed(scripted.turn_id);
}

async function appendTurnWithAnimation(turn) {
  addTurn(turn);
  const card = createTurnCard(turn, turnCtx());
  $("transcript").appendChild(card.el);
  card.el.scrollIntoView({ behavior: "smooth", block: "start" });

  const failAt = turn.error ? "generate" : (turn.guard && turn.guard.verdict === "rejected" ? "validate" : null);
  await runSimulatedStages(card.pipeline.setStage, {
    failAt,
    onStageSettled: (key) => {
      if (key === "understand") card.revealEarly();
    },
  });
  card.revealEarly();
  card.revealLate();
}

function scrollToTurn(turnId) {
  const el = document.getElementById(`turn-${turnId}`);
  if (el) {
    el.classList.remove("collapsed");
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.animate(
      [{ boxShadow: "0 0 0 3px rgba(13,148,136,.5)" }, { boxShadow: "0 0 0 0 rgba(13,148,136,0)" }],
      { duration: 900 },
    );
  }
}

/* ── Assumption editing / clarification (simulated: re-renders the same
 * scripted turn with the edited value patched in locally — there is no
 * real inference engine behind the demo, so this simulates what a
 * PATCH .../assumptions round trip would change in the UI). ─────────── */
function turnCtx() {
  return {
    onJumpToTurn: (turnId) => scrollToTurn(turnId),
    onEditAssumption: (turnId, field, value) => {
      if (state.mode === "live") {
        patchLiveAssumption(turnId, field, value);
        return;
      }
      const t = findTurn(turnId);
      if (!t) return;
      const a = t.ambiguity.assumptions.find((x) => x.field === field);
      if (a) a.value = value;
      rerenderTurn(t);
      showNotice("ok", `مفروضهٔ «${field}» به «${value}» تغییر کرد (شبیه‌سازی محلی — بدون اجرای مجدد واقعی).`);
    },
    onClarify: (turnId, field, option) => {
      const t = findTurn(turnId);
      if (!t) return;
      const a = t.ambiguity.assumptions.find((x) => x.field === field);
      if (a) { a.value = option; a.source = "question"; }
      rerenderTurn(t);
      showNotice("ok", `پاسخ شما («${option}») به‌عنوان مفروضهٔ صریح ثبت شد (شبیه‌سازی محلی).`);
    },
  };
}

function rerenderTurn(turn) {
  const old = document.getElementById(`turn-${turn.turn_id}`);
  const card = createTurnCard(turn, turnCtx());
  if (old) old.replaceWith(card.el);
  else $("transcript").appendChild(card.el);
}

/* ── Live ──────────────────────────────────────────────────────────── */
async function ensureLiveSession() {
  if (state.sessionId) return state.sessionId;
  const session = await api.createSession();
  state.sessionId = session.session_id || session.id;
  return state.sessionId;
}

async function askLive(q) {
  // First use (or after a clear/401): prompt before even attempting the
  // network call, rather than let an entirely predictable 401 round-trip
  // happen first. A stale/revoked key is still caught below, reactively,
  // by the UnauthorizedError branch of handleLiveError.
  if (!hasApiKey()) {
    promptForApiKey("برای پرسیدن سؤال در حالت زندهٔ API، ابتدا کلید API خود را وارد کنید (از مدیر سامانه بگیرید).");
    return;
  }

  let sessionId;
  try {
    sessionId = await ensureLiveSession();
  } catch (err) {
    handleLiveError(err);
    return;
  }

  // Build a placeholder card immediately so the pipeline shows "running"
  // while we wait on SSE — filled in from `stage`/`resolved`/etc. events.
  let working = emptyTurn(q, sessionId);
  const card = createTurnCard(working, turnCtx());
  $("transcript").appendChild(card.el);
  card.el.scrollIntoView({ behavior: "smooth", block: "start" });
  card.revealEarly();
  card.revealLate();

  const stageStates = {};
  // Tracks the turn_id of whatever DOM node is CURRENTLY on screen, so the
  // lookup below always targets the right element even across the "done"
  // event's swap from the placeholder id (`live_...`) to the server's real
  // turn_id. Reassigning `working` to `data.turn` (see the "done" case)
  // changes `working.turn_id` *before* this function runs; looking the old
  // node up by the NEW id would silently find nothing (optional chaining
  // swallows it) and leave the stale placeholder element in the DOM forever
  // — including every click handler still closed over the placeholder id,
  // which broke PATCH .../assumptions (it patched a turn_id that never
  // existed server-side).
  let renderedTurnId = working.turn_id;
  function rebuild() {
    const fresh = createTurnCard(working, turnCtx());
    for (const [k, v] of Object.entries(stageStates)) fresh.pipeline.setStage(k, v);
    document.getElementById(`turn-${renderedTurnId}`)?.replaceWith(fresh.el);
    renderedTurnId = working.turn_id;
    fresh.revealEarly();
    fresh.revealLate();
    Object.assign(card, fresh);
  }

  try {
    await api.askTurnStreaming(sessionId, q, (event, data) => {
      switch (event) {
        case "stage":
          stageStates[data.stage] = data.state === "running" ? "running" : data.state === "error" ? "error" : "done";
          card.pipeline.setStage(data.stage, stageStates[data.stage]);
          break;
        case "resolved":
          Object.assign(working, { resolved_question: data.resolved_question, basis: data.basis });
          rebuild();
          break;
        case "assumptions":
          working.ambiguity = data;
          rebuild();
          break;
        case "sql":
          Object.assign(working, { sql: data.sql, guard: data.guard });
          rebuild();
          break;
        case "rows":
          working.result = { columns: data.columns, rows: data.rows, row_count: data.row_count, truncated: !!data.truncated };
          rebuild();
          break;
        case "llm":
          working.llm = data;
          rebuild();
          break;
        case "done":
          working = data.turn || working;
          addTurn(working);
          rebuild();
          break;
        case "error":
          working.error = data;
          rebuild();
          break;
        default:
          break;
      }
    });
  } catch (err) {
    if (err instanceof V2NotSupportedError) {
      document.getElementById(`turn-${working.turn_id}`)?.remove();
      handleLiveError(err);
      return;
    }
    // 401 (key revoked mid-session) and 429 (throttled on the turns call
    // itself, even though session creation above succeeded) both get the
    // same distinguishable, actionable notice as everywhere else in live
    // mode — not left to render as a generic TRANSPORT_ERROR inside the
    // turn card, which is exactly the "staring at a spinner" failure mode
    // this branch exists to avoid.
    if (err instanceof UnauthorizedError || err instanceof RateLimitError) {
      document.getElementById(`turn-${working.turn_id}`)?.remove();
      handleLiveError(err);
      return;
    }
    working.error = { code: "TRANSPORT_ERROR", message: err.message };
    rebuild();
  }
}

async function patchLiveAssumption(turnId, field, value) {
  try {
    const updated = await api.patchAssumptions(state.sessionId, turnId, [{ field, value }]);
    const idx = state.turns.findIndex((t) => t.turn_id === turnId);
    if (idx >= 0) state.turns[idx] = updated;
    rerenderTurn(updated);
  } catch (err) {
    handleLiveError(err);
  }
}

function handleLiveError(err) {
  if (err instanceof V2NotSupportedError) {
    showNotice("warn", "بک‌اند نسخهٔ گفتگویی v2 را هنوز پشتیبانی نمی‌کند (404 روی /v2/sessions). حالت به «نمایشی» بازگردانده شد.");
    setMode("simulated");
    return;
  }
  // 401: distinguished from a generic ApiError so a rejected/expired key
  // never just sits there as an unexplained error — the stored key is
  // cleared and the analyst is re-prompted immediately (never shown the
  // key itself, only this message).
  if (err instanceof UnauthorizedError) {
    clearApiKey();
    promptForApiKey("کلید API رد شد یا نامعتبر است — لطفاً یک کلید جدید وارد کنید.");
    return;
  }
  // 429: client-side rate limiting, not a query or model failure — see
  // api/middleware.py's RateLimitMiddleware. Surfaced with its own
  // wording (and the retry-after it carries) instead of the generic
  // "backend error" banner, which is exactly what let this exact
  // response get misread as a query failure once already (see this
  // branch's PR description).
  if (err instanceof RateLimitError) {
    const retry = err.retryAfterSeconds != null
      ? `${Math.max(1, Math.ceil(err.retryAfterSeconds))} ثانیهٔ دیگر`
      : "چند لحظهٔ دیگر";
    showNotice("warn", `محدودیت نرخ درخواست (این خطای محدودسازی سمت کلاینت است، نه خطای پرسش یا مدل) — ${retry} دوباره امتحان کنید. ${err.message}`);
    return;
  }
  if (err instanceof ApiError) {
    showNotice("error", `خطای بک‌اند: ${err.message}`);
    return;
  }
  showNotice("error", `خطای غیرمنتظره: ${err.message}`);
}

function emptyTurn(question, sessionId) {
  return {
    turn_id: `live_${Date.now()}`,
    session_id: sessionId,
    index: state.turns.length + 1,
    question,
    resolved_question: null,
    basis: { kind: "fresh", refines_turn_id: null, composition: "none", inherited: [] },
    sql: null,
    ambiguity: { is_ambiguous: false, assumptions: [], clarifications: [] },
    guard: null,
    result: null,
    interpretation: null,
    tier: null,
    warnings: [],
    llm: null,
    timings: {},
    error: null,
  };
}

/* ── Footer clock ──────────────────────────────────────────────────── */
function tickClock() {
  $("foot-time").textContent = new Date().toLocaleTimeString("fa-IR");
}
