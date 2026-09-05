/* web/js/state.js — small app-wide state: run mode, theme, and the
 * transcript accumulated so far. No framework, just a plain object plus a
 * handful of pub/sub-free helper functions; main.js owns when to re-render. */

"use strict";

const STORAGE_THEME_KEY = "lsa-web-theme";
const STORAGE_BASE_KEY = "lsa-web-base";
const STORAGE_SESSION_KEY = "lsa-web-session-id";

export const state = {
  mode: "simulated", // "simulated" | "live"
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
