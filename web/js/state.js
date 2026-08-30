/* web/js/state.js — small app-wide state: run mode, theme, and the
 * transcript accumulated so far. No framework, just a plain object plus a
 * handful of pub/sub-free helper functions; main.js owns when to re-render. */

"use strict";

const STORAGE_THEME_KEY = "lsa-web-theme";
const STORAGE_BASE_KEY = "lsa-web-base";

export const state = {
  mode: "simulated", // "simulated" | "live"
  baseUrl: "http://localhost:8000",
  theme: "system", // "system" | "light" | "dark"
  sessionId: null,
  turns: [], // Turn[] rendered so far, in order
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
