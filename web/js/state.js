/* web/js/state.js — small app-wide state: run mode, theme, and the
 * transcript accumulated so far. No framework, just a plain object plus a
 * handful of pub/sub-free helper functions; main.js owns when to re-render. */

"use strict";

const STORAGE_THEME_KEY = "lsa-web-theme";
const STORAGE_BASE_KEY = "lsa-web-base";
const STORAGE_SESSION_KEY = "lsa-web-session-id";

export const state = {
  // "live" is the default. A deployment is live and the analysts opening
  // this page expect real answers; defaulting to "simulated" would mean
  // synthetic, made-up numbers render in the exact same UI as real ones
  // — clearly labelled, but still the wrong DEFAULT once the system is
  // actually serving people. Simulated mode stays fully available (it is
  // genuinely used for demos and training — see web/README.md's
  // "Modes"); only the default changes. See resolveBootMode below for
  // how `?live=0` / `?live=1` and the topbar's mode-switch buttons
  // override this. Consequence to hold honestly: on a first load against
  // an unreachable backend, an analyst now sees an error (from
  // refreshHealth in main.js) instead of a working simulated demo — see
  // that error's own wording for why that is the right trade.
  mode: "live", // "simulated" | "live"
  baseUrl: "http://localhost:8000",
  theme: "system", // "system" | "light" | "dark"
  sessionId: null,
  sessions: [], // SessionSummary[] — the conversation-list sidebar's index
  turns: [], // Turn[] rendered so far, in order
  memory: null, // {entries, rememberable} | null — the memory panel's last fetch
  busy: false,
  nextScriptedIndex: 0, // pointer into SCENARIO.turns for the "sample" flow
};

export function loadPersisted() {
  try {
    const t = localStorage.getItem(STORAGE_THEME_KEY);
    if (t === "light" || t === "dark" || t === "system") state.theme = t;
  } catch { /* localStorage unavailable (private mode, etc.) — keep default */ }
  try {
    const b = localStorage.getItem(STORAGE_BASE_KEY);
    if (b) state.baseUrl = b;
  } catch { /* ignore */ }
  try {
    const s = localStorage.getItem(STORAGE_SESSION_KEY);
    if (s) state.sessionId = s;
  } catch { /* ignore — reload just won't return the analyst to the same conversation */ }
}

/** Remembers which conversation the analyst was on, so a reload returns
 * them to it (see resolveActiveSessionId, which reconciles this stored id
 * against the real index once it's fetched — the id might be stale). */
export function persistSessionId(sessionId) {
  state.sessionId = sessionId;
  try {
    if (sessionId) localStorage.setItem(STORAGE_SESSION_KEY, sessionId);
    else localStorage.removeItem(STORAGE_SESSION_KEY);
  } catch { /* ignore — the choice just won't survive a reload */ }
}

/** Picks which session should be active once the real index is known.
 * Pure, total, no localStorage access (loadPersisted already read the
 * raw stored id; this just reconciles it against the index):
 *   - an empty index -> null (first-run: nothing to select yet).
 *   - a stored id still present in the index -> kept as-is.
 *   - a stored id that is stale, missing, or was never set -> the most
 *     recently active session in the index, never a thrown error and
 *     never an arbitrary (e.g. array-order) pick. */
export function resolveActiveSessionId(storedId, sessions) {
  if (!sessions || sessions.length === 0) return null;
  if (storedId && sessions.some((s) => s.session_id === storedId)) return storedId;
  const newest = sessions.slice().sort(
    (a, b) => new Date(b.last_active_at).getTime() - new Date(a.last_active_at).getTime(),
  )[0];
  return newest.session_id;
}

/** Resolves the effective run mode for THIS page load from the `?live=`
 * URL param, falling back to *defaultMode* (state.mode's own initial
 * value, "live" — see its comment above) when the param is absent or
 * unrecognized. Pure, total, no DOM/localStorage access, so the exact
 * precedence rule is unit-testable without standing up the whole page:
 *   - `?live=1` -> "live" (explicit opt-in — e.g. a bookmarked/shared link)
 *   - `?live=0` -> "simulated" (explicit opt-out — demos, training, CI)
 *   - anything else (param absent, or an unrecognized value) -> defaultMode
 * Keeping this a pure function (rather than the inline if/if it used to
 * be in main.js's boot) is what lets the DEFAULT itself change in exactly
 * one place (state.mode's initializer) without this precedence rule
 * needing to change, or be re-verified, along with it. */
export function resolveBootMode(searchParams, defaultMode) {
  const live = searchParams.get("live");
  if (live === "1") return "live";
  if (live === "0") return "simulated";
  return defaultMode;
}

/** Resolves the effective backend base URL for THIS page load. `?base=`
 * is the highest-precedence override (an operator pointing this one load
 * somewhere else for debugging) and always wins when present; otherwise
 * *currentBaseUrl* stands unchanged — by the time main.js calls this,
 * loadPersisted() has already folded in a localStorage override (if any)
 * on top of web/js/config.js's deploy-time DEFAULT_BASE_URL, so "leave it
 * alone" here already means "the persisted value, or the deploy default".
 * Pure, total, no DOM access. */
export function resolveBootBaseUrl(searchParams, currentBaseUrl) {
  const base = searchParams.get("base");
  return base ? base : currentBaseUrl;
}

export function persistTheme(theme) {
  state.theme = theme;
  try {
    localStorage.setItem(STORAGE_THEME_KEY, theme);
  } catch { /* ignore — theme just won't survive a reload */ }
}

export function persistBaseUrl(url) {
  state.baseUrl = url;
  try {
    localStorage.setItem(STORAGE_BASE_KEY, url);
  } catch { /* ignore */ }
}

export function applyTheme() {
  const root = document.documentElement;
  if (state.theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", state.theme);
}

export function addTurn(turn) {
  state.turns.push(turn);
  return turn;
}

export function findTurn(turnId) {
  return state.turns.find((t) => t.turn_id === turnId) || null;
}

export function resetTranscript() {
  state.turns = [];
  state.nextScriptedIndex = 0;
}
