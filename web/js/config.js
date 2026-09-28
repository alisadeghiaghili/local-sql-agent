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
 *
 * DEFAULT_API_PORT backs a second way to point this UI at its backend,
 * used whenever DEFAULT_BASE_URL is left empty: the API address is then
 * derived from THIS PAGE's own protocol and hostname plus this port
 * (see web/js/state.js's resolveDefaultBaseUrl) rather than a fixed
 * host baked in here. That is what lets a deployment move the API+UI
 * pair to a new host — the real incident this exists for: an operator
 * moved both to 172.16.101.42, edited this file's DEFAULT_BASE_URL by
 * hand, and it still pointed at the old host — with no edit to this
 * file at all, as long as the API is reachable on the same hostname the
 * page itself was loaded from. Both web/ (this file) and web/admin/
 * (web/admin/main.js) resolve their default the same way, from the same
 * two constants, so moving host is a one-line env change
 * (CORS_ALLOWED_ORIGINS aside), never a JS edit on two pages.
 */

"use strict";

export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_API_PORT = 8000;
