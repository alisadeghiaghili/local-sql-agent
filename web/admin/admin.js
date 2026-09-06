/* web/admin/admin.js
 *
 * Request layer for the admin panel. Phase 1 (read-only observability --
 * see docs/admin-panel-architecture.md and the frozen phase-1 spec it
 * implements a slice of) made every call here a `GET /admin/*` route on
 * purpose: this panel started as a dashboard, not a tool
 * (docs/admin-panel-architecture.md §3.1 / the phase-1 spec's "What this
 * phase must not do"). Phase 4 adds the one deliberate exception --
 * `resolveFeedback`, a real write -- because the whole point of the
 * triage queue is that a human resolves a flag; see that method's own
 * comment for why it is still safe under §3.1's "no free-form write
 * surface" spirit (it can only ever record a fixed, closed-set decision,
 * never touch domain knowledge or the guard's allowlist directly).
 *
 * Credentials: reuses web/js/apikey.js's per-analyst key storage
 * unchanged -- an admin key is just a key with the `admin` capability
 * (security.auth.Principal.is_admin), so there is no second credential
 * store here (the phase-1 spec is explicit about this). Every request
 * below routes through the single `_get` chokepoint, which is what
 * `tests/web_ui/run_admin_auth_boundary.mjs` drives directly under Node
 * to prove every admin call attaches `Authorization: Bearer <key>` --
 * mirrors web/js/api.js's own `_fetchV2` chokepoint and the test that
 * covers it (tests/web_ui/run_auth_boundary.mjs).
 */

"use strict";

import { getApiKey } from "../js/apikey.js";

export class AdminApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
  }
}

/** A 401: no credential was presented/accepted at all. */
export class AdminUnauthorizedError extends AdminApiError {
  constructor(message) {
    super(message, 401);
    this.name = "AdminUnauthorizedError";
  }
}

/** A 403: the credential is real, but it is not an admin key
 * (security.auth.Principal.is_admin is false -- api/errors.py's
 * ADMIN_REQUIRED). Distinguished from AdminUnauthorizedError so the panel
 * can say plainly "this key is not an admin key" instead of showing an
 * empty dashboard -- the phase-1 spec's own requirement. */
export class AdminForbiddenError extends AdminApiError {
  constructor(message) {
    super(message, 403);
    this.name = "AdminForbiddenError";
  }
}

export class AdminApi {
  /** @param {string} baseUrl */
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  /** GET /admin/summary -- scripts.analyze_audit_log.build_report's report.
   * Defaults to the aggregate-safe mode server-side; `includeExamples`
   * only ever opts IN to the verbatim-examples mode, never the reverse. */
  async summary(includeExamples = false) {
    const qs = includeExamples ? "?include_examples=true" : "";
    return this._get(`/admin/summary${qs}`);
  }

  /** GET /admin/health/checks -- scripts.verify_deployment's checks, run now. */
  async healthChecks() {
    return this._get("/admin/health/checks");
  }

  /** GET /admin/cache -- the existing query-result cache statistics. */
  async cache() {
    return this._get("/admin/cache");
  }

  /** GET /admin/config -- which project_config/ files loaded, and how
   * many entries each yielded. Never file contents. */
  async config() {
    return this._get("/admin/config");
  }

  /* ── Admin panel phase 4: the wrong-answer feedback triage queue ────
   * The one place this file's own "read-only, no method here changes
   * server state" rule (see module docstring) is deliberately no longer
   * true: resolveFeedback (below) is the panel's first write. Reachable
   * only by the operations/security capability
   * (api.auth.require_operations_or_security), same as every /admin
   * write route -- see api/admin_feedback_routes.py. */

  /** GET /admin/feedback?status=... -- the triage queue, joined to each
   * flag's audit record (question, SQL, guard verdict, assumptions). */
  async feedbackList(status) {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return this._get(`/admin/feedback${qs}`);
  }

  /** GET /admin/feedback/stats -- flag volume, outcomes and the golden
   * set's size/pending count (spec §5's "closing the loop visibly"). */
  async feedbackStats() {
    return this._get("/admin/feedback/stats");
  }

  /** POST /admin/feedback/{id}/resolve -- resolve one flag into exactly
   * one outcome (spec §3.1). Never creates or applies a configuration
   * version itself, and never writes to the golden set except through
   * the server's own promotion path for the "golden_case" outcome --
   * this call only ever sends the admin's *decision*. */
  async resolveFeedback(feedbackId, { outcome, note, configVersionId, tags }) {
    return this._post(`/admin/feedback/${encodeURIComponent(feedbackId)}/resolve`, {
      outcome,
      note: note || "",
      config_version_id: configVersionId ?? null,
      tags: tags || [],
    });
  }

  /** The one chokepoint every admin call above routes through. Always
   * attaches `Authorization: Bearer <key>` when a key is stored (never
   * omitted for an admin route -- unlike web/js/api.js's health(), there
   * is no unauthenticated admin route to degrade to); a call made with no
   * key stored is still attempted, so the server's real 401 (not a
   * client-side guess) is what tells the operator a key is needed. */
  async _get(path) {
    const headers = {};
    const key = getApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;

    let res;
    try {
      res = await fetch(`${this.baseUrl}${path}`, { headers, signal: AbortSignal.timeout(20000) });
    } catch (err) {
      throw new AdminApiError(`Network error calling ${path}: ${err.message}`, 0);
    }

    if (res.status === 401) {
      const body = await _safeJson(res);
      throw new AdminUnauthorizedError(
        body?.error?.message || body?.detail || "Missing or invalid API key.",
      );
    }
    if (res.status === 403) {
      const body = await _safeJson(res);
      throw new AdminForbiddenError(
        body?.error?.message || body?.detail || "This key does not have admin access.",
      );
    }
    if (!res.ok) {
      const body = await _safeJson(res);
      throw new AdminApiError(body?.error?.message || body?.detail || `HTTP ${res.status}`, res.status);
    }
    return res.json();
  }

  /** Same auth/error handling as `_get`, for the one write call this
   * panel makes (`resolveFeedback` above). */
  async _post(path, jsonBody) {
    const headers = { "Content-Type": "application/json" };
    const key = getApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;

    let res;
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers,
        body: JSON.stringify(jsonBody),
        signal: AbortSignal.timeout(20000),
      });
    } catch (err) {
      throw new AdminApiError(`Network error calling ${path}: ${err.message}`, 0);
    }

    if (res.status === 401) {
      const body = await _safeJson(res);
      throw new AdminUnauthorizedError(
        body?.error?.message || body?.detail || "Missing or invalid API key.",
      );
    }
    if (res.status === 403) {
      const body = await _safeJson(res);
      throw new AdminForbiddenError(
        body?.error?.message || body?.detail || "This key does not have admin access.",
      );
    }
    if (!res.ok) {
      const body = await _safeJson(res);
      throw new AdminApiError(body?.error?.message || body?.detail || `HTTP ${res.status}`, res.status);
    }
    return res.json();
  }
}

async function _safeJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}
