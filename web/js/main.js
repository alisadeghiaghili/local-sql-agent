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

import {
  SCENARIO, SCENARIO_MATCH_HINTS,
  SCENARIO_SESSIONS, SCENARIO_MEMORY, getSimulatedSessionTurns, FULL_TURNS_BY_ID,
} from "./data.js";
import {
  state, loadPersisted, persistTheme, persistSessionId, persistInterpret,
  applyTheme, addTurn, findTurn, resetTranscript, resolveActiveSessionId,
  resolveBootMode, resolveBootBaseUrl, resolveDefaultBaseUrl, normalizeBaseUrl,
} from "./state.js";
import { Api, V2NotSupportedError, ApiError, UnauthorizedError, RateLimitError } from "./api.js";
import { setApiKey, clearApiKey, hasApiKey } from "./apikey.js";
import {
  CODE_UNEXPECTED_PAYLOAD, createDiagnoser, hideDiagnosisBanner, isApiHealthShape, showDiagnosisBanner,
} from "./diagnosis.js";
import { DEFAULT_BASE_URL, DEFAULT_API_PORT } from "./config.js";
import { createTurnCard, FAILURE_BY_CODE } from "./render/turn.js";
import { canConfirm, renderConfirmationControl } from "./render/confirmation.js";
import { runSimulatedStages } from "./render/pipeline.js";
import { renderSessionList } from "./render/sessions.js";
import { memoryKeyForField, renderMemoryPanel } from "./render/memory.js";
import { ensureTsqlPrismReady } from "./sql-display.js";
import { t, loadLang, setLang, applyLang } from "./i18n.js";
import { initDashboard, syncDashboard } from "./dashboard.js";

const $ = (id) => document.getElementById(id);

/** Every "bring this into view" call in this module routes through here.
 * style.css already forces `scroll-behavior: auto` under
 * `prefers-reduced-motion: reduce` for CSS-driven scrolling (e.g. anchor
 * jumps), but `Element.scrollIntoView({behavior: "smooth"})` is a
 * script-requested behaviour and, per spec, an explicit "smooth" wins over
 * that CSS property rather than deferring to it — so a hardcoded "smooth"
 * here would silently defeat the reduced-motion rule for the one motion
 * an analyst cannot opt out of by ignoring the page: their own submitted
 * question relocating the viewport out from under them. */
function scrollIntoViewMaybeSmooth(el, opts) {
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ ...opts, behavior: reduced ? "auto" : "smooth" });
}

/** D3-hidden-chart: the `{ block: "start" }` scroll that fires the moment a
 * turn card is appended (below) only ever sees the card's *early* content —
 * the result (a chart, table, etc.) is still `hidden` at that point
 * (turn.js's tagLate/revealLate) and gets unhidden afterward, once the
 * pipeline settles. A chart is tall (~440px measured), so that late reveal
 * routinely grows the card past the bottom of the viewport, and nothing
 * re-scrolls to account for it — it just sits there, entirely covered by
 * the sticky composer (measured 0px of a 440px chart visible; see
 * style.css's --composer-clearance comment for the full numbers).
 *
 * Call this once a turn's late content is actually in the DOM (right after
 * `revealLate()`) to close that gap, passing `card.result` (turn.js) —
 * NOT `card.el` (the whole turn card). A first version targeted `card.el`
 * with `block: "end"`: the *card* keeps going well past the result
 * (interpretation, then the collapsed pipeline/LLM/feedback drawer), so
 * aligning the card's bottom with the viewport scrolled the chart itself
 * off the TOP of the screen — visible measurement: the svg ended up at
 * y=-347, above the fold, not above the composer. `card.result` is just
 * the "نتیجه" card, which is short enough that `block: "nearest"` — only
 * scroll if it is not already fully visible — is both correct and the
 * gentler choice: a turn that already fits does not get an extra jump on
 * top of the start-scroll above. `.turn`'s `scroll-margin-bottom:
 * var(--composer-clearance)` (style.css) is what makes "fully visible"
 * mean "visible above the composer" rather than merely on screen — since
 * `card.result` sits inside `.turn`, it inherits that clearance. Falls
 * back to `el` itself (the whole card) for a turn with no result (an
 * error or a guard rejection), where there is nothing else to target. */
function scrollSettledResultAboveComposer(el) {
  if (el) scrollIntoViewMaybeSmooth(el, { block: "nearest" });
}

/** Park the newest turn above the pinned assistant panel — the "scroll to
 * bottom on Enter" behaviour. Targets the last `.turn` in the transcript
 * (turns always append at the foot), so the just-asked Q&A is brought up to
 * the panel no matter where the analyst was scrolled. `scroll-padding-bottom`
 * on `<html>` (style.css) reserves the panel's --composer-clearance, so
 * block:"end" stops the turn's bottom clear of the sticky panel, not under it. */
function scrollLatestTurnIntoView() {
  const turns = document.querySelectorAll("#transcript .turn");
  const el = turns.length ? turns[turns.length - 1] : null;
  if (el) scrollIntoViewMaybeSmooth(el, { block: "end" });
}

// Declared before the boot section: setMode(state.mode) runs at module load
// and, in simulated mode, calls setHealth() synchronously — so the label map
// setHealth reads must already be initialized here, not in the module's lower
// half. Declaring it below the boot call put it in the temporal dead zone at
// boot, which threw a ReferenceError and killed the entire simulated UI.
const HEALTH_STATE_LABEL_KEY = { ok: "healthStateUp", down: "healthStateDown", unknown: "healthStateUnknown" };

// Declared here for the same reason: refreshHealth() runs from the boot call
// below. Decides, once per failed status check, whether the address this page
// calls is the API at all (diagnosis.js), so the page can say so in one banner
// instead of three red lights and a tooltip.
const diagnoser = createDiagnoser({
  getBaseUrl: () => state.baseUrl,
  getPageOrigin: () => location.origin,
});
let healthCheckSeq = 0;
// The last three lights painted by an answer, kept so first-hand evidence
// (a live turn that came back) can correct a stale probe without inventing
// states for the lights it says nothing about. See noteLiveTurnAnswered().
let lastHealth = { api: null, llm: null, db: null, label: "" };
// Has any /health answer (success or failure) painted the lights yet?
// The heart must not sit neutral while an unreachable dependency makes the
// request hang, so boot watches this and raises the alarm if it stays false.
let healthAnswered = false;

/* ── Boot ──────────────────────────────────────────────────────────── */
// T-SQL Prism patch must run before the first Prism.tokenize — Prism
// expands greedy grammar rules in place on first use. Patching after a
// highlight has already run makes the next one throw. See sql-display.js.
ensureTsqlPrismReady();

const params = new URLSearchParams(location.search);
// Deploy-time default (web/js/config.js's DEFAULT_BASE_URL, or — when
// that is left empty — this page's own protocol/host plus
// DEFAULT_API_PORT, see resolveDefaultBaseUrl) first; loadPersisted()
// then overrides it with a localStorage value if one was ever saved
// (e.g. an operator debugging against a different backend from this
// browser) — loadPersisted() itself now repairs/discards a bad saved
// value via normalizeBaseUrl rather than loading it verbatim.
// resolveBootBaseUrl's `?base=` param is the final, highest-precedence
// override, for a one-off load; it is validated below the same way
// before being trusted. Same layering for mode: state.js's own default
// ("live" — see its comment) unless `?live=0`/`?live=1` says otherwise.
// Run mode is URL-only now, decided once at boot; there is no topbar
// control left that can change it afterward.
state.baseUrl = resolveDefaultBaseUrl(DEFAULT_BASE_URL, DEFAULT_API_PORT, location);
loadPersisted();
state.mode = resolveBootMode(params, state.mode);
state.lang = loadLang();
applyTheme();
applyLang(state.lang);
updateThemeQuickToggle();

const rawBootBaseUrl = resolveBootBaseUrl(params, state.baseUrl);
if (rawBootBaseUrl !== state.baseUrl) {
  // A `?base=` param was actually supplied for this load -- validate it
  // instead of trusting it silently (the same
  // "172.16.101.42:8076"-style incident this normalisation exists for,
  // just arriving via the URL instead of a typed field). An invalid
  // value is REJECTED, not guessed at: whatever state.baseUrl already
  // resolved to (the persisted value, or the deploy default) stands.
  const result = normalizeBaseUrl(rawBootBaseUrl);
  if (result.ok) {
    state.baseUrl = result.url;
  } else {
    showNotice("error", `${t("invalidBaseUrlNotice")} (?base=${rawBootBaseUrl})`);
  }
}

let api = new Api(state.baseUrl);

// turn_id -> Turn[], keyed by session_id: keeps a conversation's in-tab
// progress when the analyst switches away and back, so flipping between
// sessions in the sidebar never discards what was just asked (see
// switchToSession). Session-local to this tab; never persisted.
const turnsCache = new Map();

// Turn ids the signed-in analyst has confirmed ("پاسخ درست است") in the
// sessions opened so far -- what each turn card's confirmation toggle reads
// its state from, so re-rendering a card (or switching away and back) shows
// the same state. Turn ids are unique, so one set serves every session.
// Declared here, above the boot calls below, for the reason
// HEALTH_STATE_LABEL_KEY is: a const read before its line has run is a
// ReferenceError (see tests/web_ui/test_web_ui_boot_order.py).
const confirmedTurnIds = new Set();

wireComposer();
wireTopbar();
wireSidebar();
wireRailMenu();
wireMemoryPanel();
initDashboard();
setMode(state.mode);
setTimeout(() => {
  if (!healthAnswered) setHealth(null, null, null, t("systemStatusAlert"));
}, 3000);
// The lights used to be painted ONCE, at boot: open the page before the API
// is up (or lose it for a minute) and a red alarm stayed red for the rest of
// the session, even after answers were coming back. One slow re-probe keeps
// the heart honest without touching anything else; noteLiveTurnAnswered()
// corrects it sooner, the first time a real answer lands.
setInterval(() => {
  if (state.mode === "live") refreshHealth();
}, 60000);

/* ── Theme / user menu ─────────────────────────────────────────────── */
function wireTopbar() {
  wireUserMenu();
  wireThemeQuickToggle();

  $("live-key-save").addEventListener("click", () => {
    const val = $("live-key-input").value;
    if (!val.trim()) return;
    setApiKey(val);
    $("live-key-input").value = "";
    updateKeyStatus();  // leaves the "rejected" state: a new key was given
    showNotice("ok", t("apiKeyStoredNotice"));
    refreshHealth();
  });

  // Per-analyst, persisted, and read fresh on every ask (see askLive).
  const interpretToggle = $("interpret-toggle");
  interpretToggle.checked = state.interpret;
  interpretToggle.addEventListener("change", () => {
    persistInterpret(interpretToggle.checked);
  });

  $("live-key-change").addEventListener("click", () => {
    // Reveals the entry field without touching the stored key, so a
    // mistyped replacement does not leave the analyst with neither.
    $("live-key-entry").hidden = false;
    $("live-key-change").hidden = true;
    $("live-key-input").focus();
  });

  $("live-key-clear").addEventListener("click", () => {
    clearApiKey();
    $("live-key-input").value = "";
    updateKeyStatus();
    showNotice("warn", t("apiKeyClearedNotice"));
  });
  updateKeyStatus();
  updateThemeSegment();
  updateLangSegment();
}

/** ADR-004 part 1: populate the account menu's "درخواست‌های دسترسی"
 * (access requests) status list -- the minimal place an analyst can see
 * their own requests' status and, once denied, the reason. Reuses the
 * account/menu area (theme/language/API key already live there) instead
 * of adding a new panel, and refreshes on every menu open rather than
 * polling, since this list only ever changes as a side effect of the
 * analyst's own actions (submitting a request) or an admin's (approving/
 * denying one) elsewhere.
 *
 * LIVE mode only -- SIMULATED mode has no server-side request store to
 * read (turnCtx().onRequestAccess never actually submits one in that
 * mode either). Every value rendered here (column name, denial reason) is
 * server-supplied and untrusted -- built with `textContent` assignments
 * only, the same rule turn.js's own denied-column rendering follows;
 * never string-built HTML.
 */
async function refreshAccessRequestsList() {
  const host = $("access-requests-list");
  if (!host) return;

  const showEmpty = (message) => {
    host.innerHTML = "";
    const p = document.createElement("p");
    p.className = "access-requests-empty";
    p.textContent = message;
    host.appendChild(p);
  };

  if (state.mode !== "live") {
    showEmpty(t("accessRequestsEmpty"));
    return;
  }

  const STATUS_LABELS = {
    open: t("accessRequestStatusOpen"),
    approved: t("accessRequestStatusApproved"),
    denied: t("accessRequestStatusDenied"),
  };

  try {
    const { access_requests: rows } = await api.listAccessRequests();
    if (!rows || !rows.length) {
      showEmpty(t("accessRequestsEmpty"));
      return;
    }
    host.innerHTML = "";
    for (const row of rows) {
      const item = document.createElement("div");
      item.className = "access-request-item";

      const columnEl = document.createElement("span");
      columnEl.className = "access-request-column";
      columnEl.textContent = row.column_name;
      item.appendChild(columnEl);

      const statusEl = document.createElement("span");
      statusEl.className = `access-request-status access-request-status-${row.status}`;
      statusEl.textContent = STATUS_LABELS[row.status] || row.status;
      item.appendChild(statusEl);

      if (row.status === "denied" && row.resolution_note) {
        const reasonEl = document.createElement("p");
        reasonEl.className = "access-request-reason";
        reasonEl.textContent = row.resolution_note;
        item.appendChild(reasonEl);
      }

      host.appendChild(item);
    }
  } catch (err) {
    // A missing/unsupported endpoint or a network error must not break
    // the rest of the menu -- shown as the same empty-state paragraph,
    // with the real message when there is one.
    showEmpty((err && err.message) || t("accessRequestsEmpty"));
  }
}

/** User menu: theme, language, API key — one popover, not topbar clutter. */
function wireUserMenu() {
  const btn = $("user-menu-btn");
  const panel = $("user-menu-panel");
  if (!btn || !panel) return;

  const close = () => {
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  };
  const open = () => {
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    refreshAccessRequestsList();
  };

  btn.addEventListener("click", () => {
    if (panel.hidden) open();
    else close();
  });
  document.addEventListener("click", (e) => {
    if (panel.hidden) return;
    if (!$("user-menu").contains(/** @type {Node} */ (e.target))) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) {
      close();
      btn.focus();
    }
  });

  $("theme-segment").querySelectorAll("button[data-theme-val]").forEach((b) => {
    b.addEventListener("click", () => {
      const val = b.getAttribute("data-theme-val");
      if (val === "system" || val === "light" || val === "dark") {
        persistTheme(val);
        applyTheme();
        updateThemeSegment();
        updateThemeQuickToggle();
      }
    });
  });

  $("lang-segment").querySelectorAll("button[data-lang]").forEach((b) => {
    b.addEventListener("click", () => {
      const lang = b.getAttribute("data-lang");
      if (lang === "fa" || lang === "en") {
        state.lang = lang;
        setLang(lang);
        updateLangSegment();
        updateKeyStatus();
        updateThemeQuickToggle();
        setMode(state.mode); // refresh foot strings in the new language
        // A turn card's chrome (panel titles, its buttons) is built once,
        // reading the language at render time — rebuild the cards so every
        // one of them follows the switch instead of keeping the old wording.
        renderTranscriptFromTurns(state.turns);
        // The memory panel's controls ("Save" / the select placeholder) read
        // the language the same way; refresh it if it happens to be open.
        if ($("memory-panel") && !$("memory-panel").hidden && state.memory) {
          renderMemoryPanelBody(state.memory);
        }
      }
    });
  });
}

function updateThemeSegment() {
  document.querySelectorAll("#theme-segment button[data-theme-val]").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-theme-val") === state.theme);
  });
}

/** One-press light/dark switch in the topbar. The three-way choice
 * (including "system") stays in the account menu; this button is the
 * shortcut an analyst reaches for, and it always commits an explicit
 * theme, so what they pick survives a reload. */
function updateThemeQuickToggle() {
  const btn = $("theme-quick-toggle");
  if (!btn) return;
  const explicit = document.documentElement.getAttribute("data-theme");
  const effective = explicit
    || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const turningToLight = effective === "dark";
  btn.setAttribute("aria-label", t("themeQuick"));
  btn.title = t("themeQuick");
  const sun = btn.querySelector(".icon-sun");
  const moon = btn.querySelector(".icon-moon");
  // Show the destination, not the current state: a sun means "switch to light".
  if (sun) sun.hidden = !turningToLight;
  if (moon) moon.hidden = turningToLight;
}

function wireThemeQuickToggle() {
  const btn = $("theme-quick-toggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const explicit = document.documentElement.getAttribute("data-theme");
    const effective = explicit
      || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    persistTheme(effective === "dark" ? "light" : "dark");
    applyTheme();
    updateThemeSegment();
    updateThemeQuickToggle();
  });
  updateThemeQuickToggle();
}

function updateLangSegment() {
  document.querySelectorAll("#lang-segment button[data-lang]").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-lang") === state.lang);
  });
}

/* ── API key state (topbar) ────────────────────────────────────────
 * Three states, because "a key is stored" and "the server accepts it"
 * are different facts and the difference is actionable:
 *
 *   stored    -> pill + "تغییر کلید". No entry field: an empty password
 *                box is what a signed-OUT page shows, and this one is
 *                always empty (it is cleared after a save), so leaving it
 *                on screen told every analyst they were logged out.
 *   unset     -> entry field, no clear/change button (nothing to clear).
 *   rejected  -> entry field AND the stored key kept. The key is printed
 *                exactly once by scripts/issue_api_key.py and is not
 *                recoverable, so a 401 -- which can be transient, e.g. a
 *                server restarted mid-configuration -- must not destroy
 *                the analyst's only copy of it. They can retype it, or
 *                clear it deliberately.
 */
function updateKeyStatus(rejected = false) {
  const el = $("live-key-status");
  const entry = $("live-key-entry");
  const change = $("live-key-change");
  const clear = $("live-key-clear");
  const stored = hasApiKey();

  if (rejected) {
    el.textContent = t("apiKeyRejected");
    el.className = "live-key-status unset";
  } else if (stored) {
    el.textContent = t("apiKeySaved");
    el.className = "live-key-status set";
  } else {
    el.textContent = t("apiKeyUnset");
    el.className = "live-key-status unset";
  }

  const showEntry = rejected || !stored;
  entry.hidden = !showEntry;
  change.hidden = showEntry;
  clear.hidden = !stored;
}

/** Open the user menu and focus the API-key input — used on first live
 * use and after a 401. Never puts the key itself into the input. */
function promptForApiKey(message, { rejected = false } = {}) {
  const panel = $("user-menu-panel");
  const btn = $("user-menu-btn");
  if (panel && btn) {
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }
  updateKeyStatus(rejected);
  showNotice("warn", message);
  $("live-key-input").focus();
}

/* ── Mode switch ───────────────────────────────────────────────────── */
function setMode(mode) {
  state.mode = mode;
  updateKeyStatus();

  if (mode === "simulated") {
    setHealth(true, true, true, "simulated");
    hideDiagnosisBanner($("api-diagnosis"));
  } else {
    refreshHealth();
  }

  refreshSessionsForMode();
}

// The dot alone carried the state for nobody but a reader who both sees
// colour and can tell #10b981 apart from #e5484d at a glance. WCAG 1.4.1:
// colour may never be the *only* carrier. Two populations got nothing from
// the old markup — a colour-blind analyst staring at the topbar, and a
// screen-reader user for whom #health's role="status" aria-live="polite"
// read "API LLM DB" on every poll, identically, whatever the state. The
// fix is the state word itself, in two places: as real, visible text next
// to the name (so a colour-blind reader sees the difference, not just a
// hue) and as the pill's aria-label (so the live region announces "API:
// down" instead of re-reading three names that never change). Values from
// /health (h.llmDetail, h.dbDetail, forwarded to setHealth as `label`) are
// never interpolated into markup here — see DESIGN-INVARIANTS.md §1.3.
function setHealth(api_, llm, db, label) {
  lastHealth = { api: api_, llm, db, label };
  const dot = (ok) => (ok === null ? "unknown" : ok ? "ok" : "down");
  const host = $("health");
  host.replaceChildren(
    ...[["API", api_], ["LLM", llm], ["DB", db]].map(([name, ok]) => {
      const state = dot(ok);
      const stateLabel = t(HEALTH_STATE_LABEL_KEY[state]);

      const dotEl = document.createElement("span");
      dotEl.className = `dot dot-${state}`;

      const nameEl = document.createElement("span");
      nameEl.className = "pill-name";
      nameEl.textContent = name;

      const stateEl = document.createElement("span");
      stateEl.className = "pill-state";
      stateEl.textContent = stateLabel;

      const pill = document.createElement("span");
      pill.className = "pill";
      pill.append(dotEl, nameEl, stateEl);
      // aria-label wins over the pill's own text nodes as its accessible
      // name, which is what keeps the live-region announcement a clean
      // "API: down" rather than a run-on of three separately-read spans.
      pill.setAttribute("aria-label", `${name}: ${stateLabel}`);
      return pill;
    }),
  );
  host.title = label;
  healthAnswered = true;

  // The topbar no longer shows three pills: the heart carries the whole
  // state. Green when every dependency is up; a blinking red alarm when
  // even one is down (or still unverified). The panel behind the heart
  // still lists all three.
  const statusHeart = $("status-btn");
  if (statusHeart) {
    const allUp = api_ === true && llm === true && db === true;
    statusHeart.classList.toggle("is-ok", allUp);
    statusHeart.classList.toggle("is-alert", !allUp);
    const key = allUp ? "systemStatusOk" : "systemStatusAlert";
    statusHeart.setAttribute("aria-label", t(key));
    statusHeart.title = t(key);
  }
}

async function refreshHealth() {
  const seq = ++healthCheckSeq;
  try {
    const h = await api.health();
    // A 200 whose body is not the API's health response is some other
    // service answering at this address, not a healthy API: judged here,
    // not trusted as "API up".
    if (!isApiHealthShape(h.raw)) {
      throw new ApiError("/health did not return the API's health response", 200, CODE_UNEXPECTED_PAYLOAD);
    }
    // The tooltip carries the backend's own reason for each light, so a
    // red LLM indicator says WHY rather than leaving the reader to guess
    // between a wrong host, a wrong key, and an endpoint we never call.
    const lines = [`/health: ${h.status}, model ${h.model || "?"}`];
    if (h.llmDetail) lines.push(`LLM: ${h.llmDetail}`);
    if (h.dbDetail) lines.push(`DB: ${h.dbDetail}`);
    setHealth(h.api, h.llm, h.db, lines.join("\n"));
    hideDiagnosisBanner($("api-diagnosis"));
  } catch (err) {
    // TRANSPORT_ERROR (api.js's health()) means the fetch itself never
    // got a response -- unreachable host, or (the incident this branch
    // exists for) a CORS rejection that looks identical to it from the
    // page's point of view. err.message already carries
    // describeTransportFailure's bilingual, actionable hint (naming this
    // page's own origin and CORS_ALLOWED_ORIGINS); a real HTTP error
    // response (e.g. 500) falls through to the plain fallback instead,
    // since that is a live backend, not a transport problem.
    const hint = err && err.code === "TRANSPORT_ERROR" && err.message
      ? err.message
      : "بک‌اند در دسترس نیست — uvicorn api.server:app را اجرا کنید یا حالت نمایشی را انتخاب کنید";
    setHealth(false, false, false, hint);
    // The tooltip above is read by nobody who does not hover the lights; a
    // wrong address or a refused page is said once, in the banner.
    diagnoser.reset();
    const diagnosis = await diagnoser.diagnose(err);
    // A newer check started while this one was probing: its answer wins.
    if (seq === healthCheckSeq) showDiagnosisBanner($("api-diagnosis"), diagnosis, "analyst");
  }
}

/** A finished live turn is first-hand proof that the backend, the model and
 * the database all answered: SQL came back from the model and rows came back
 * from the warehouse. The lights used to be painted ONLY by /health, whose
 * model probe asks the provider for a `/models` listing — a question a
 * perfectly working OpenAI-compatible server may not implement — so the
 * page could sit behind a blinking red alarm while the analyst asked
 * questions and got answers. Evidence from a real answer wins over that
 * probe; the next /health poll still has the final word (refreshHealth).
 */
function noteLiveTurnAnswered() {
  if (lastHealth.api === true && lastHealth.llm === true && lastHealth.db === true) return;
  setHealth(true, true, true, t("systemStatusOk"));
}

/* ── Sessions (sidebar) ────────────────────────────────────────────────
 * The sidebar's own index (`state.sessions`) and the active transcript
 * (`state.turns`) are two separate concerns: the index lists every
 * conversation (title, recency, turn count); the transcript is whichever
 * ONE conversation is currently open. Switching sessions never mutates
 * the index itself, only which transcript is loaded. */

function renderSessionSidebar() {
  const host = $("session-list-host");
  host.innerHTML = "";
  host.appendChild(renderSessionList(state.sessions, {
    selectedId: state.sessionId,
    onSelect: (sessionId) => { if (!state.busy) switchToSession(sessionId); },
    onRename: (sessionId, title) => renameSession(sessionId, title),
    onDelete: (sessionId) => deleteSessionAndFollowUp(sessionId),
  }));
  // The dashboard reads the same resolved state the rail does; it never
  // fetches anything of its own (see dashboard.js's docstring).
  syncDashboard({
    sessions: state.sessions,
    turns: state.turns,
    mode: state.mode,
    baseUrl: state.baseUrl,
    sessionId: state.sessionId,
  });
}

/** (Re)resolves and loads the active session for the CURRENT mode.
 * Called on boot and every time the mode switch is clicked — simulated
 * and live modes each have their own, entirely separate, session index. */
async function refreshSessionsForMode() {
  if (state.mode === "live") {
    if (!hasApiKey()) {
      state.sessions = [];
      // Without this, switching from simulated to live-without-a-key left
      // whatever simulated transcript (and its sample-story chips — B5)
      // was already on screen just sitting there under the "live" mode
      // switch, which is exactly the demo-scaffolding-in-production leak
      // this decision exists to close.
      resetTranscript();
      renderTranscriptFromTurns([]);
      renderSessionSidebar();
      return;
    }
    try {
      const res = await api.listSessions();
      state.sessions = res.sessions || [];
    } catch {
      // No v2 support yet, unreachable backend, a blocked cross-origin
      // request, or an expired key — the sidebar just shows no
      // conversations rather than an error banner; askLive's own error
      // handling covers the moment the analyst actually tries to do
      // something.
      //
      // The active session MUST be cleared, not merely left alone. This
      // used to `return` here with state.sessionId untouched, so a failure
      // to list left whatever was active before — and if the analyst had
      // been in simulated mode, that was a scripted id from data.js. The
      // next question then went to the real backend as
      // POST /v2/sessions/s_demo_1404/turns: a demo id, invented locally,
      // sent to a server that has never heard of it.
      //
      // Clearing it means ensureLiveSession() opens a real one on the next
      // question, which is the only correct thing to do when this mode's
      // own index could not be read.
      state.sessions = [];
      state.sessionId = null;
      persistSessionId(null);
      resetTranscript();
      renderTranscriptFromTurns([]);
      renderSessionSidebar();
      return;
    }
  } else {
    state.sessions = SCENARIO_SESSIONS.map((s) => ({ ...s }));
  }

  // Every page load opens on a clean, fresh conversation — like pressing
  // "new query" — rather than restoring the analyst's last session. The
  // full index still renders in the sidebar so any prior conversation is
  // one click away. (resolveActiveSessionId is kept for the post-delete
  // follow-up path, not for boot.)
  state.sessionId = null;
  persistSessionId(null);
  resetTranscript();
  renderTranscriptFromTurns([]);
  renderSessionSidebar();
  // The ring assumption chips read their option list from the rememberable
  // memory set; loading it quietly at boot (no DOM, no panel) keeps those
  // selects populated the moment a historical turn is opened.
  loadAndRenderMemory({ quiet: true });
}

/** Loads *sessionId*'s transcript into the transcript pane. Stashes the
 * outgoing session's turns in `turnsCache` first (so switching back
 * later restores exactly what was there, instead of refetching and
 * losing anything asked in this tab), then either restores from that
 * cache or loads fresh — from the simulated scenario data, or from the
 * real GET /v2/sessions/{sid} transcript endpoint in live mode. */
async function switchToSession(sessionId) {
  if (sessionId === state.sessionId && state.turns.length > 0) {
    renderSessionSidebar();
    return;
  }
  if (state.sessionId) turnsCache.set(state.sessionId, state.turns);
  persistSessionId(sessionId);

  // An empty cached transcript carries no information — restoring it only
  // hides turns the server does have (and a double click on the same row
  // used to cache its own empty list, then "restore" it, skipping the
  // fetch entirely). Cache hits must have something to show.
  const cached = turnsCache.get(sessionId);
  if (cached && cached.length) {
    state.turns = cached;
  } else if (state.mode === "live") {
    try {
      const session = await api.getSession(sessionId);
      state.turns = session.turns || [];
    } catch (err) {
      handleLiveError(err);
      state.turns = [];
    }
    await loadConfirmedTurns(sessionId);
  } else {
    state.turns = getSimulatedSessionTurns(sessionId);
  }
  state.nextScriptedIndex = 0;
  renderTranscriptFromTurns(state.turns);
  renderSessionSidebar();
}

/** Fills confirmedTurnIds from the server for one session. Best effort: a
 * failure leaves the toggles unpressed rather than blocking the transcript
 * (the server stays the source of truth, and pressing a toggle is
 * idempotent). */
async function loadConfirmedTurns(sessionId) {
  try {
    const found = await api.listConfirmations(sessionId);
    for (const id of found.turn_ids || []) confirmedTurnIds.add(id);
  } catch {
    // Not fatal; see above.
  }
}

/** Replaces the whole transcript pane with *turns*, already-resolved (no
 * pipeline animation — these are turns from switching sessions, not a
 * freshly asked question). The dashboard's ask card leads the page, so an
 * empty transcript stays empty: no placeholder is rendered (the helpers
 * below it remain for the composer's sample-story bookkeeping). */
function renderTranscriptFromTurns(turns) {
  const host = $("transcript");
  host.innerHTML = "";
  for (const turn of turns) {
    const card = createTurnCard(turn, turnCtx());
    host.appendChild(card.el);
  }
  refreshSampleButtonsDisabledState();
  syncDashboard({
    sessions: state.sessions,
    turns: state.turns,
    mode: state.mode,
    baseUrl: state.baseUrl,
    sessionId: state.sessionId,
  });
}

/** The empty-conversation placeholder — DESIGN.md §5.1's resolution for
 * the sample-story chips that used to sit inside the composer card
 * permanently (overlapping #question's placeholder text) and, per
 * decision B5, have no business appearing in a live production session
 * at all. Rebuilt from scratch on every call rather than toggled with
 * `hidden`, since it must reflect whichever mode is CURRENT, not
 * whichever mode last rendered it. Not rendered any more: the dashboard's
 * ask card leads the page, so an empty transcript shows nothing rather
 * than a placeholder card (kept alongside the sample-chip bookkeeping
 * that renderSamples/renderStartExamples still serve). */
function renderTranscriptEmptyState() {
  const empty = document.createElement("div");
  empty.className = "transcript-empty";
  empty.id = "transcript-empty";

  const title = document.createElement("h2");
  title.textContent = t("transcriptEmptyTitle");
  empty.appendChild(title);

  const hint = document.createElement("p");
  hint.className = "transcript-empty-hint";
  hint.textContent = t("transcriptEmptyHint");
  empty.appendChild(hint);

  if (state.mode === "simulated") {
    const wrap = document.createElement("div");
    wrap.className = "samples";
    wrap.id = "samples";
    const label = document.createElement("span");
    label.className = "samples-label";
    label.textContent = t("samples");
    wrap.appendChild(label);
    empty.appendChild(wrap);
    renderSamples(wrap);
  } else if (state.mode === "live" && hasApiKey()) {
    // Questions this deployment's examples.yaml curates, offered as
    // one-click starts. Filled in when they arrive; if they never do (an
    // older server, no examples, an error), the hint above is the whole
    // empty state, as before.
    const wrap = document.createElement("div");
    wrap.className = "samples start-examples";
    wrap.id = "start-examples";
    wrap.hidden = true;
    empty.appendChild(wrap);
    renderStartExamples(wrap);
  }

  $("transcript").appendChild(empty);
}

/** The start questions, fetched once per page load (they change only when
 * the operator edits examples.yaml); a failed fetch is not remembered, so
 * the next empty conversation tries again. */
let startExamplesPromise = null;

async function renderStartExamples(wrap) {
  if (!startExamplesPromise) {
    startExamplesPromise = api.listExamples(6).then(
      (body) => (Array.isArray(body && body.examples) ? body.examples : []),
      () => { startExamplesPromise = null; return []; },
    );
  }
  const examples = await startExamplesPromise;
  if (!wrap.isConnected || !examples.length) return;
  const label = document.createElement("span");
  label.className = "samples-label";
  label.textContent = t("startExamples");
  wrap.appendChild(label);
  for (const ex of examples) {
    const question = typeof ex.question === "string" ? ex.question : "";
    if (!question) continue;
    const btn = document.createElement("button");
    btn.className = "sample start-example";
    btn.type = "button";
    btn.textContent = question;
    btn.dir = "auto";
    btn.addEventListener("click", () => {
      if (state.busy) return;
      $("question").value = question;
      ask();
    });
    wrap.appendChild(btn);
  }
  wrap.hidden = false;
}

/** Removes the empty-state placeholder (and, with it, any sample-story
 * chips) the instant a turn is about to land in the transcript — called
 * right before appendTurnWithAnimation/askLive append the new turn/
 * placeholder card, since state.turns itself is not always updated yet
 * at that point (askLive's placeholder predates the "done" event's
 * addTurn call). */
function clearTranscriptEmptyState() {
  document.getElementById("transcript-empty")?.remove();
}

/** A sample button is disabled once its scripted turn has been asked in
 * ANY session this tab has touched (turnsCache + the active session) —
 * these are one-shot "story" prompts, not per-conversation state. */
function refreshSampleButtonsDisabledState() {
  const usedIds = new Set(state.turns.map((t) => t.turn_id));
  for (const cached of turnsCache.values()) for (const t of cached) usedIds.add(t.turn_id);
  document.querySelectorAll(".sample").forEach((btn) => {
    btn.disabled = usedIds.has(btn.dataset.turnId);
  });
}

async function renameSession(sessionId, title) {
  if (state.mode === "live") {
    try {
      const updated = await api.renameSession(sessionId, title);
      const idx = state.sessions.findIndex((s) => s.session_id === sessionId);
      if (idx >= 0) state.sessions[idx] = { ...state.sessions[idx], ...updated };
      renderSessionSidebar();
    } catch (err) {
      handleLiveError(err);
    }
    return;
  }
  const idx = state.sessions.findIndex((s) => s.session_id === sessionId);
  if (idx >= 0) {
    state.sessions[idx] = { ...state.sessions[idx], title };
    renderSessionSidebar();
  }
}

async function deleteSessionAndFollowUp(sessionId) {
  if (state.mode === "live") {
    try {
      await api.deleteSession(sessionId);
    } catch (err) {
      handleLiveError(err);
      return;
    }
  }
  state.sessions = state.sessions.filter((s) => s.session_id !== sessionId);
  turnsCache.delete(sessionId);

  if (state.sessionId !== sessionId) {
    renderSessionSidebar();
    return;
  }
  const next = resolveActiveSessionId(null, state.sessions);
  if (next) {
    await switchToSession(next);
  } else {
    state.sessionId = null;
    persistSessionId(null);
    resetTranscript();
    renderTranscriptFromTurns([]);
    renderSessionSidebar();
  }
}

async function createNewSession() {
  const nowIso = new Date().toISOString();
  if (state.mode === "live") {
    if (!hasApiKey()) {
      promptForApiKey("برای ساخت گفتگوی جدید در حالت زندهٔ API، ابتدا کلید API خود را وارد کنید.");
      return;
    }
    try {
      const session = await api.createSession();
      const sid = session.session_id || session.id;
      state.sessions.unshift({
        session_id: sid, title: "گفتگوی جدید", created_at: nowIso, last_active_at: nowIso,
        turn_count: 0, expires_at: session.expires_at || null,
      });
      turnsCache.set(sid, []);
      await switchToSession(sid);
    } catch (err) {
      handleLiveError(err);
    }
    return;
  }
  const sid = `s_local_${Date.now()}`;
  state.sessions.unshift({
    session_id: sid, title: "گفتگوی جدید", created_at: nowIso, last_active_at: nowIso,
    turn_count: 0, expires_at: null,
  });
  turnsCache.set(sid, []);
  await switchToSession(sid);
}

/** Reflects a just-completed turn back into the sidebar's own summary
 * (turn count, recency) — the index and the transcript would otherwise
 * silently drift apart the moment the very first question is asked. */
function bumpActiveSessionMeta() {
  const idx = state.sessions.findIndex((s) => s.session_id === state.sessionId);
  if (idx < 0) return;
  state.sessions[idx] = {
    ...state.sessions[idx], turn_count: state.turns.length, last_active_at: new Date().toISOString(),
  };
  renderSessionSidebar();
}

function wireSidebar() {
  $("btn-new-session").addEventListener("click", createNewSession);
  $("sidebar-toggle").addEventListener("click", () => {
    const body = document.querySelector(".app-body");
    const open = body.classList.toggle("sidebar-open");
    $("sidebar-toggle").setAttribute("aria-expanded", String(open));
  });
}

/* ── Rail settings menu ────────────────────────────────────────────────
 * The rail is one icon; its destinations are rows in this popover. Each
 * row IS the original control (same id), so the handlers above and in
 * dashboard.js keep working untouched — this only opens and closes it. */
function wireRailMenu() {
  const btn = $("rail-settings");
  const menu = $("nav-menu");
  if (!btn || !menu) return;

  const setMenu = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };

  btn.addEventListener("click", () => setMenu(menu.hidden));
  // Choosing a destination closes the menu; the row's own handler runs first.
  menu.addEventListener("click", (event) => {
    if (event.target.closest(".nav-menu-item")) setMenu(false);
  });
  document.addEventListener("click", (event) => {
    if (menu.hidden) return;
    if (!event.target.closest("#nav-menu, #rail-settings")) setMenu(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setMenu(false);
      btn.focus();
    }
  });
}

/* ── Memory panel ──────────────────────────────────────────────────────
 * Reachable from the topbar, beside the API-key controls. Memory is
 * created EXPLICITLY only — the panel's own "set" controls, or a "📌 به
 * خاطر بسپار" pin on an editable assumption chip (see turnCtx's onPin
 * below) — never inferred by this UI. */

function wireMemoryPanel() {
  $("memory-toggle").addEventListener("click", () => {
    if ($("memory-panel").hidden) openMemoryPanel();
    else closeMemoryPanel();
  });
  $("memory-panel-close").addEventListener("click", closeMemoryPanel);
  $("memory-overlay").addEventListener("click", closeMemoryPanel);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("memory-panel").hidden) closeMemoryPanel();
  });
}

async function openMemoryPanel() {
  $("memory-panel").hidden = false;
  $("memory-overlay").hidden = false;
  $("memory-toggle").setAttribute("aria-expanded", "true");
  await loadAndRenderMemory();
}

function closeMemoryPanel() {
  $("memory-panel").hidden = true;
  $("memory-overlay").hidden = true;
  $("memory-toggle").setAttribute("aria-expanded", "false");
}

async function loadAndRenderMemory(opts = {}) {
  if (state.mode === "live") {
    if (!hasApiKey()) {
      if (opts.quiet) return;
      promptForApiKey("برای مشاهدهٔ حافظهٔ تحلیلی، ابتدا کلید API خود را وارد کنید.");
      renderMemoryPanelBody({ entries: [], rememberable: [] });
      return;
    }
    try {
      state.memory = await api.getMemory();
    } catch (err) {
      handleLiveError(err);
      return;
    }
  } else if (!state.memory) {
    state.memory = JSON.parse(JSON.stringify(SCENARIO_MEMORY));
  }
  // `quiet` is for the pin path, which needs the rememberable set loaded
  // but must not disturb a panel the analyst has not opened.
  if (!opts.quiet) renderMemoryPanelBody(state.memory);
}

function renderMemoryPanelBody(memory) {
  const host = $("memory-panel-host");
  host.innerHTML = "";
  host.appendChild(renderMemoryPanel(memory, {
    onSetValue: (key, value, errorEl) => setMemoryValue(key, value, errorEl),
    onClearOne: (key) => clearMemoryEntry(key),
    onClearAll: () => clearAllMemory(),
  }));
}

async function setMemoryValue(key, value, errorEl) {
  if (state.mode === "live") {
    try {
      await api.putMemory(key, value);
      state.memory = await api.getMemory();
      renderMemoryPanelBody(state.memory);
    } catch (err) {
      // A 422 (bad value) renders inline, next to the field it came
      // from — not as a page-level notice — since the panel is the only
      // context that makes it actionable. Anything else (401/429/network)
      // gets the usual page-level handling.
      if (err instanceof ApiError && !(err instanceof UnauthorizedError) && !(err instanceof RateLimitError)) {
        errorEl.textContent = err.message;
      } else {
        handleLiveError(err);
      }
    }
    return;
  }
  upsertSimulatedMemory(key, value);
  renderMemoryPanelBody(state.memory);
}

async function clearMemoryEntry(key) {
  if (state.mode === "live") {
    try {
      await api.deleteMemoryEntry(key);
      state.memory = await api.getMemory();
    } catch (err) {
      handleLiveError(err);
      return;
    }
  } else {
    state.memory.entries = state.memory.entries.filter((e) => e.key !== key);
  }
  renderMemoryPanelBody(state.memory);
}

async function clearAllMemory() {
  if (state.mode === "live") {
    try {
      await api.clearMemory();
      state.memory = await api.getMemory();
    } catch (err) {
      handleLiveError(err);
      return;
    }
  } else {
    state.memory.entries = [];
  }
  renderMemoryPanelBody(state.memory);
}

/** Simulated-mode-only: upserts one memory entry locally, matching the
 * field label from `rememberable` when the key is a known one (the panel's
 * own "set" controls always are; a pinned assumption's field usually is
 * too, but falls back to using the field name verbatim if not). */
function upsertSimulatedMemory(key, value) {
  if (!state.memory) state.memory = JSON.parse(JSON.stringify(SCENARIO_MEMORY));
  const knownField = state.memory.rememberable.find((r) => r.key === key);
  const field = (knownField && knownField.field) || key;
  const entry = { key, field, value, updated_at: new Date().toISOString(), applicable: true };
  const idx = state.memory.entries.findIndex((e) => e.key === key);
  if (idx >= 0) state.memory.entries[idx] = entry;
  else state.memory.entries.push(entry);
}

/* ── Composer ──────────────────────────────────────────────────────── */
/** Populates *wrap* (the empty-transcript state's .samples container —
 * see renderTranscriptEmptyState) with one button per scripted turn. */
function renderSamples(wrap) {
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
    // A new turn was just added (or the input cleared on an error): bring the
    // transcript's bottom — and the newest Q&A — above the pinned panel.
    scrollLatestTurnIntoView();
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
  clearTranscriptEmptyState();
  const card = createTurnCard(turn, turnCtx());
  $("transcript").appendChild(card.el);
  scrollIntoViewMaybeSmooth(card.el, { block: "start" });

  const failAt = turn.error ? "generate" : (turn.guard && turn.guard.verdict === "rejected" ? "validate" : null);
  await runSimulatedStages(card.pipeline.setStage, {
    failAt,
    onStageSettled: (key) => {
      if (key === "understand") card.revealEarly();
    },
  });
  card.revealEarly();
  card.revealLate();
  scrollSettledResultAboveComposer(card.result || card.el);
  bumpActiveSessionMeta();
}

function scrollToTurn(turnId) {
  const el = document.getElementById(`turn-${turnId}`);
  if (el) {
    el.classList.remove("collapsed");
    scrollIntoViewMaybeSmooth(el, { block: "start" });
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
      if (state.mode === "live") {
        patchLiveAssumption(turnId, field, option);
        return;
      }
      const t = findTurn(turnId);
      if (!t) return;
      const a = t.ambiguity.assumptions.find((x) => x.field === field);
      if (a) { a.value = option; a.source = "question"; }
      rerenderTurn(t);
      showNotice("ok", `پاسخ شما («${option}») به‌عنوان مفروضهٔ صریح ثبت شد (شبیه‌سازی محلی).`);
    },
    // "پین کردن" — the ONLY way memory gets created. Never inferred.
    onPin: (turnId, field, value) => {
      if (state.mode === "live") pinLiveAssumption(field, value);
      else pinSimulatedAssumption(field, value);
    },
    // The `ring` assumption renders as a <select> whose options mirror the
    // memory panel's closed set (rememberable key "ring"). Returns null when
    // the memory set isn't loaded yet, in which case the chip falls back to
    // its plain text + edit-button form.
    getRingOptions: () => {
      const rememberable = state.memory && state.memory.rememberable;
      if (!Array.isArray(rememberable)) return null;
      const ring = rememberable.find((r) => r && r.key === "ring");
      return ring && Array.isArray(ring.options) && ring.options.length ? ring.options : null;
    },
    // "دوباره اجرا کن" — the rows_omitted re-run affordance (turn.js's
    // result block). Not a PATCH/re-fetch of this exact turn (the
    // contract has no such endpoint); it re-asks the same question,
    // which is the closest honest equivalent to "run it again".
    onRerun: (turnId) => rerunTurn(turnId),
    // "ویرایش پرسش" — a guard-rejected turn's failure state (turn.js's
    // renderFailureState). Deliberately NOT onRerun: that re-submits the
    // exact same question, which would just be rejected again by the
    // same PolicyRejection. This puts the original question back in the
    // box and stops — an analyst edits it before sending, they don't get
    // it resent for them.
    onRephrase: (turnId) => {
      const t = findTurn(turnId);
      if (!t) return;
      $("question").value = t.question;
      $("question").focus();
      scrollIntoViewMaybeSmooth($("question"), { block: "center" });
    },
    // "پرسش بدون «ستون»" — the targeted action DESIGN-INVARIANTS.md §8's
    // table specifies for a denied-column refusal ("Ask without that
    // column"), offered only when the guard named exactly one column
    // (turn.js reads `turn.guard.subject`). Mirrors onRephrase's own
    // model — puts an edited question back in the box and stops, rather
    // than resubmitting for the analyst — but seeds an explicit exclusion
    // instruction instead of the bare original question: resending the
    // identical question would just be denied again by the same policy.
    onAskWithoutColumn: (turnId, column) => {
      const t = findTurn(turnId);
      if (!t) return;
      $("question").value = `${t.question} (بدون ستون «${column}»)`;
      $("question").focus();
      scrollIntoViewMaybeSmooth($("question"), { block: "center" });
    },
    // "درخواست دسترسی" (ADR-004 part 1) -- the denied-column failure
    // state's second action, beside "پرسش بدون «ستون»"
    // (DESIGN-INVARIANTS.md §8: "Ask without that column · Request
    // access"). LIVE mode really submits it, scoped to the session this
    // transcript is currently showing; the requester is stamped
    // server-side and the column is re-derived server-side from the audit
    // record, so this call sends nothing but the turn's own identifiers.
    // SIMULATED mode has no server to send it to, so it is accepted
    // locally and shown as such -- mirrors onFlag's own simulated-mode
    // behaviour just below.
    onRequestAccess: async (turnId, _column) => {
      if (state.mode === "live") {
        if (!state.sessionId) throw new Error("هیچ گفتگوی زنده‌ای در جریان نیست.");
        return await api.submitAccessRequest(state.sessionId, turnId);
      }
      showNotice(
        "ok",
        "در حالت نمایشی، درخواست‌های دسترسی به سرور واقعی ارسال نمی‌شوند (فقط شبیه‌سازی محلی).",
      );
      return { already_pending: false };
    },
    // "این عدد درست نیست" (admin panel phase 4). LIVE mode really submits
    // it to the backend, against the session this transcript is currently
    // showing; SIMULATED mode has no server to send it to, so it is
    // accepted locally and shown as such rather than pretending to
    // round-trip a demo scenario through a real endpoint.
    onFlag: async (turnId, category, note) => {
      if (state.mode === "live") {
        if (!state.sessionId) throw new Error("هیچ گفتگوی زنده‌ای در جریان نیست.");
        await api.submitFeedback(state.sessionId, turnId, category, note);
        // The server drops this principal's confirmation of the same turn
        // (the later action wins); the local record follows.
        confirmedTurnIds.delete(turnId);
        return;
      }
      showNotice("ok", "در حالت نمایشی، گزارش‌ها به سرور واقعی ارسال نمی‌شوند (فقط شبیه‌سازی محلی).");
    },
    // "پاسخ درست است" -- confirms an answer; the positive twin of onFlag.
    // LIVE mode really submits it; SIMULATED mode has no server, so it is
    // accepted locally and shown as such, like onFlag above.
    renderConfirmation: (turn, { onReplacedFlag }) => {
      if (!canConfirm(turn)) return null;
      const turnId = turn.turn_id;
      return renderConfirmationControl(turn, {
        confirmed: confirmedTurnIds.has(turnId),
        onConfirm: async () => {
          if (state.mode === "live") {
            if (!state.sessionId) throw new Error("هیچ گفتگوی زنده‌ای در جریان نیست.");
            const result = await api.confirmAnswer(state.sessionId, turnId);
            confirmedTurnIds.add(turnId);
            return result;
          }
          confirmedTurnIds.add(turnId);
          showNotice("ok", "در حالت نمایشی، تأییدها به سرور واقعی ارسال نمی‌شوند (فقط شبیه‌سازی محلی).");
          return { removed_flags: 0 };
        },
        onWithdraw: async () => {
          if (state.mode === "live") {
            if (!state.sessionId) throw new Error("هیچ گفتگوی زنده‌ای در جریان نیست.");
            await api.withdrawConfirmation(state.sessionId, turnId);
          }
          confirmedTurnIds.delete(turnId);
        },
        // The server removed this analyst's open flag on the turn (the later
        // action wins); the flag control mirrors that.
        onConfirmed: (result) => {
          const replaced = Boolean(result && result.removed_flags > 0);
          if (replaced) onReplacedFlag();
          return replaced;
        },
      });
    },
  };
}

function rerenderTurn(turn) {
  const old = document.getElementById(`turn-${turn.turn_id}`);
  const card = createTurnCard(turn, turnCtx());
  if (old) {
    old.replaceWith(card.el);
  } else {
    clearTranscriptEmptyState();
    $("transcript").appendChild(card.el);
    // A turn with no card yet lands at the foot of the transcript: in live
    // mode, answering a clarification or editing an assumption re-asks the
    // question and the server returns it as a NEW turn (TurnEngine.ask
    // appends it with its own turn_id). Bring the analyst to its answer,
    // as a streamed answer does once it settles (#219).
    scrollSettledResultAboveComposer(card.result || card.el);
    scrollLatestTurnIntoView();
  }
}

function rerunTurn(turnId) {
  const t = findTurn(turnId);
  if (!t) return;
  if (state.mode === "live") {
    $("question").value = t.question;
    ask();
    return;
  }
  rerunSimulatedTurn(turnId);
}

/** Simulated-mode re-run: restores the turn's REAL data (the version it
 * had before rows were stripped for the "reopened conversation" demo —
 * see data.js's getSimulatedSessionTurns/FULL_TURNS_BY_ID) rather than
 * re-running the ask() pipeline, since askSimulated's own dedup check
 * would just report "already answered" for a turn_id already in
 * state.turns — which is exactly this one. */
function rerunSimulatedTurn(turnId) {
  const full = FULL_TURNS_BY_ID[turnId];
  if (!full) {
    showNotice("info", "بازاجرای این نوبت در نسخهٔ نمایشی پشتیبانی نمی‌شود.");
    return;
  }
  const restored = JSON.parse(JSON.stringify(full));
  const idx = state.turns.findIndex((t) => t.turn_id === turnId);
  if (idx >= 0) state.turns[idx] = restored;
  rerenderTurn(restored);
  showNotice("ok", "دادهٔ این نوبت دوباره اجرا و بارگذاری شد.");
}

async function pinLiveAssumption(field, value) {
  if (!hasApiKey()) {
    promptForApiKey("برای به‌خاطر سپردن این مقدار، ابتدا کلید API خود را وارد کنید.");
    return;
  }
  try {
    // The analyst can pin without ever having opened the memory panel, so
    // the rememberable set may not be loaded yet. Fetch it on demand
    // rather than assuming the panel populated it.
    const rem = () => (state.memory && state.memory.rememberable) || [];
    if (!memoryKeyForField(rem(), field)) await loadAndRenderMemory({ quiet: true });
    const key = memoryKeyForField(rem(), field);
    if (key === null) {
      showNotice(
        "warn",
        `«${field}» جزو مواردی نیست که بتوان به‌خاطر سپرد. فهرست موارد قابل ذخیره در پنل حافظه است.`,
      );
      return;
    }
    await api.putMemory(key, value);
    showNotice("ok", `مفروضهٔ «${field}: ${value}» به‌عنوان اولویت ثابت ذخیره شد — از این پس در گفتگوهای بعدی هم اعمال می‌شود.`);
    if (!$("memory-panel").hidden) await loadAndRenderMemory();
  } catch (err) {
    handleLiveError(err);
  }
}

function pinSimulatedAssumption(field, value) {
  upsertSimulatedMemory(field, value);
  showNotice("ok", `مفروضهٔ «${field}: ${value}» به‌عنوان اولویت ثابت ذخیره شد (شبیه‌سازی محلی) — از این پس در گفتگوهای بعدی هم اعمال می‌شود.`);
  if (!$("memory-panel").hidden) renderMemoryPanelBody(state.memory);
}

/* ── Live ──────────────────────────────────────────────────────────── */
async function ensureLiveSession() {
  if (state.sessionId) return state.sessionId;
  const session = await api.createSession();
  const sid = session.session_id || session.id;
  persistSessionId(sid);
  const nowIso = new Date().toISOString();
  state.sessions.unshift({
    session_id: sid, title: "گفتگوی جدید", created_at: nowIso, last_active_at: nowIso,
    turn_count: 0, expires_at: session.expires_at || null,
  });
  renderSessionSidebar();
  return sid;
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
  clearTranscriptEmptyState();
  const card = createTurnCard(working, turnCtx());
  $("transcript").appendChild(card.el);
  scrollIntoViewMaybeSmooth(card.el, { block: "start" });
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
          // `sql_display` is the server's layout of `sql` (absent from an
          // older server): shown as it is until `done` brings the same
          // text, instead of the client formatter's different layout.
          Object.assign(working, { sql: data.sql, sql_display: data.sql_display, guard: data.guard });
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
          // rebuild() just Object.assign()'d `fresh` (including its
          // `result`/`el`) onto `card`, so these reflect the just-rendered
          // final turn -- no separate DOM lookup needed.
          scrollSettledResultAboveComposer(card.result || card.el);
          bumpActiveSessionMeta();
          // Not an error turn: the model produced the SQL and the database
          // produced the rows, whatever /health last claimed.
          if (working && !working.error && (working.sql || working.result)) noteLiveTurnAnswered();
          break;
        case "error":
          working.error = data;
          rebuild();
          break;
        default:
          break;
      }
    }, { interpret: state.interpret });
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
    // A real error code from the server (`api.js`'s `ApiError.code`, e.g.
    // "MAINTENANCE_MODE" from `require_not_in_maintenance` on this very
    // route, or "INJECTION_ATTEMPT") is preserved here instead of being
    // relabelled — turn.js's `FAILURE_BY_CODE` renders the right Persian
    // sentence for it. Only a genuine pre-response failure (no `code` at
    // all, or `err` is not even an ApiError) falls back to
    // "TRANSPORT_ERROR", which is what it actually was.
    const code = (err instanceof ApiError && err.code) || "TRANSPORT_ERROR";
    working.error = { code, message: err.message };
    rebuild();
  }
}

async function patchLiveAssumption(turnId, field, value) {
  try {
    const updated = await api.patchAssumptions(state.sessionId, turnId, [{ field, value }]);
    // The server re-asks the question and answers with a new turn (a new
    // turn_id, appended to the session); the turn it re-asked keeps its
    // card, so it keeps its record too. A reply under the same turn_id
    // still replaces that turn in place.
    const idx = state.turns.findIndex((t) => t.turn_id === updated.turn_id);
    if (idx >= 0) state.turns[idx] = updated;
    else state.turns.push(updated);
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
    // Deliberately does NOT clear the stored key. It was printed once by
    // scripts/issue_api_key.py and is unrecoverable, and a 401 is not
    // always the key's fault -- a server restarted with a different
    // API_KEYS_JSON, or an app database briefly unreachable, produces one
    // from a perfectly good key. Destroying it here made a transient
    // server condition into "find that 43-character string again".
    promptForApiKey(
      "سرور کلید ذخیره‌شده را نپذیرفت (۴۰۱). اگر کلید درست است، ممکن است " +
      "سرور تازه ری‌استارت شده باشد؛ دوباره امتحان کنید. در غیر این صورت " +
      "کلید تازه‌ای وارد کنید — کلید فعلی پاک نشده است.",
      { rejected: true },
    );
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
    // A recognised backend code (MAINTENANCE_MODE, SERVER_OVERLOAD,
    // INJECTION_ATTEMPT, ...) gets the same Persian sentence a turn-card
    // failure banner would show — `render/turn.js`'s `FAILURE_BY_CODE`,
    // the one table for both. "TRANSPORT_ERROR" is deliberately excluded
    // here: outside a turn card this message is `describeTransportFailure`'s
    // own diagnostic (host/CORS guess), not backend text, and it is worth
    // more to an analyst here than the generic sentence would be.
    const entry = err.code && err.code !== "TRANSPORT_ERROR" ? FAILURE_BY_CODE[err.code] : null;
    showNotice("error", entry ? entry.lead : `خطای بک‌اند: ${err.message}`);
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
