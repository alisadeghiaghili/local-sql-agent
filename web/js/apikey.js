/* web/js/apikey.js — per-analyst API key storage (Phase 8 auth).
 *
 * docs/api-contract-v2.md §11: every route except GET /health requires
 * `Authorization: Bearer <key>`. This UI is a static file served straight
 * to the browser (see web/README.md — no build step, no bundler), so any
 * credential baked into it at serve time would be readable by anyone who
 * opens dev tools: that would mean shipping one shared warehouse
 * credential to every visitor, in the repo or the served bundle.
 *
 * Instead each analyst enters their own key once; it lives only in this
 * browser's localStorage and is sent as a bearer token on every
 * authenticated call (see api.js). This is not a workaround for auth, it
 * is what makes the identity model behind auth actually work for a
 * multi-analyst UI:
 *
 * - observability/audit.py records `principal_id` on every query. One
 *   shared UI key would make every analyst's question look like the same
 *   actor in the audit trail.
 * - api/middleware.py's RateLimitMiddleware buckets on (principal, ip).
 *   One shared key behind one UI host collapses every analyst using it
 *   into a single rate-limit bucket.
 *
 * The key itself is never logged, never put in a URL/query string, and
 * never echoed back inside an error message rendered by this UI.
 */

"use strict";

const STORAGE_KEY = "lsa-web-api-key";

/** @returns {string|null} the stored key, or null if none is set / storage is unavailable. */
export function getApiKey() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v ? v : null;
  } catch {
    return null; // private mode / storage disabled — degrade to "no key"
  }
}

/** @returns {boolean} whether a key is currently stored. */
export function hasApiKey() {
  return getApiKey() !== null;
}

/** Store *key* (trimmed). A falsy/empty key clears storage instead. */
export function setApiKey(key) {
  const trimmed = (key || "").trim();
  try {
    if (trimmed) localStorage.setItem(STORAGE_KEY, trimmed);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore — key just won't survive a reload */
  }
}

/** Remove the stored key entirely (e.g. after a 401, or user-initiated). */
export function clearApiKey() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}
