/* web/js/config.js — the ONE file a deployment edits.
 *
 * DEFAULT_BASE_URL is the backend base URL used whenever this page load
 * has no more specific override — no `?base=` query param (highest
 * precedence, for one-off debugging) and no value already saved to this
 * browser's `localStorage` (see web/js/state.js's persistBaseUrl/
 * loadPersisted — an operator can still point a single browser somewhere
 * else without touching this file).
 *
 * The backend-URL row used to be a visible top-bar control every analyst
 * saw and could edit (`#live-base-row`). That was deployment
 * configuration masquerading as something an analyst should touch — a
 * wrong value there looks EXACTLY like a dead backend, and there is no
 * way for an analyst to tell the two apart from inside the page. It has
 * been removed from the top bar (see index.html / main.js); this file is
 * what a deployment now edits once, at deploy time, instead.
 *
 * This is a static file served straight to the browser (see
 * web/README.md — no build step), so this value is PUBLIC: never put a
 * credential or anything secret in it. The analyst's own identity still
 * goes through the API-key field (web/js/apikey.js) — that field is
 * deliberately untouched by this change.
 */

"use strict";

export const DEFAULT_BASE_URL = "http://localhost:8000";
