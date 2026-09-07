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

  /* ── Admin panel phase 6: the operational tier -- maintenance mode,
   * schema drift, vocabulary freshness, per-analyst usage, cache
   * controls, failed-auth visibility. Every write below (maintenance
   * toggle, vocabulary refresh, cache clear/invalidate) is recorded in
   * the admin-action trail server-side -- nothing here bypasses that. */

  /** GET /admin/maintenance -- always reachable, regardless of state. */
  async maintenanceState() {
    return this._get("/admin/maintenance");
  }

  /** POST /admin/maintenance -- switch maintenance mode on or off. */
  async setMaintenance(active, note) {
    return this._post("/admin/maintenance", { active, note: note || null });
  }

  /** GET /admin/schema-drift -- read-only; never applies anything. */
  async schemaDrift() {
    return this._get("/admin/schema-drift");
  }

  /** GET /admin/vocabulary -- per prefetched column freshness/failure state. */
  async vocabularyStatus() {
    return this._get("/admin/vocabulary");
  }

  /** POST /admin/vocabulary/{table}/{column}/refresh -- an operator's own refresh. */
  async vocabularyRefresh(table, column) {
    return this._post(
      `/admin/vocabulary/${encodeURIComponent(table)}/${encodeURIComponent(column)}/refresh`,
      {},
    );
  }

  /** GET /admin/usage -- per-principal queries/failures/latency/rate-limit hits. */
  async usage(since, until) {
    const qs = new URLSearchParams();
    if (since) qs.set("since", since);
    if (until) qs.set("until", until);
    const query = qs.toString();
    return this._get(`/admin/usage${query ? `?${query}` : ""}`);
  }

  /** POST /admin/cache/clear -- the panel must show the pre-clear cost
   * BEFORE calling this (see main.js's confirm flow); this call still
   * echoes that same snapshot in its response. */
  async cacheClear() {
    return this._post("/admin/cache/clear", {});
  }

  /** POST /admin/cache/invalidate -- evict a single cached entry. */
  async cacheInvalidate(question, mode) {
    return this._post("/admin/cache/invalidate", { question, mode: mode || "full" });
  }

  /** GET /admin/security/auth-failures -- count and source breakdown. */
  async authFailures(windowSeconds) {
    const qs = windowSeconds ? `?window_seconds=${encodeURIComponent(windowSeconds)}` : "";
    return this._get(`/admin/security/auth-failures${qs}`);
  }

  /* ── Admin panel phase 7: key lifecycle and roles ──────────────────
   * The routes behind these have existed since phase 2
   * (api/admin_write_routes.py) with no UI calling them, so issuing a key
   * or granting a role meant the CLI plus a server restart. These are the
   * panel's first writes that change *who can get in*, so each one is
   * capability-gated server-side -- operations for the key lifecycle,
   * security for anything that changes what a key can see
   * (docs/admin-panel-architecture.md §2's dividing rule) -- and the panel
   * never guesses at that gate client-side: it makes the call and reports
   * the server's own 403. */

  /** GET /admin/keys -- every key row's metadata. Never a raw key: those
   * exist only in the response to the issue call that minted them. */
  async listKeys() {
    return this._get("/admin/keys");
  }

  /** POST /admin/keys -- mint a key. The response carries `raw_key`, the
   * ONLY time it is ever transmitted; the panel must show it once and
   * hold it nowhere. Note the server's restrictive default: the new key's
   * denied_columns is every column in schema.yaml, so it authenticates
   * but can read nothing until a security admin loosens it. */
  async issueKey(principalId, name) {
    return this._post("/admin/keys", { principal_id: principalId, name });
  }

  /** POST /admin/keys/{hash}/disable -- reversible (operations). */
  async disableKey(keySha256) {
    return this._post(`/admin/keys/${encodeURIComponent(keySha256)}/disable`);
  }

  /** POST /admin/keys/{hash}/enable -- undoes a disable (operations). */
  async enableKey(keySha256) {
    return this._post(`/admin/keys/${encodeURIComponent(keySha256)}/enable`);
  }

  /** POST /admin/keys/{hash}/revoke -- permanent (operations). The row is
   * tombstoned, never deleted, so restoring the application database to a
   * point before this call cannot silently un-revoke a leaked key. */
  async revokeKey(keySha256) {
    return this._post(`/admin/keys/${encodeURIComponent(keySha256)}/revoke`);
  }

  /** PATCH /admin/keys/{hash}/acl -- the column ACL (security only). An
   * empty list means no column restriction at all. */
  async updateKeyAcl(keySha256, deniedColumns) {
    return this._patch(`/admin/keys/${encodeURIComponent(keySha256)}/acl`, {
      denied_columns: deniedColumns,
    });
  }

  /** GET /admin/roles/{capability} -- who holds it, from either source
   * (environment bootstrap or a database grant). Readable by either admin
   * role: seeing who holds a role is not itself a visibility change. */
  async roleHolders(capability) {
    return this._get(`/admin/roles/${encodeURIComponent(capability)}`);
  }

  /** POST /admin/roles/{principalId} -- grant or revoke (security only).
   * The server refuses with 409 rather than removing the last holder of
   * a capability, so the panel surfaces that message as-is. */
  async changeRole(principalId, capability, grant) {
    return this._post(`/admin/roles/${encodeURIComponent(principalId)}`, {
      capability,
      grant,
    });
  }

  /** The one chokepoint every admin call above routes through. Always
   * attaches `Authorization: Bearer <key>` when a key is stored (never
   * omitted for an admin route -- unlike web/js/api.js's health(), there
   * is no unauthenticated admin route to degrade to); a call made with no
   * key stored is still attempted, so the server's real 401 (not a
   * client-side guess) is what tells the operator a key is needed.
   *
   * `_get`, `_post` and `_patch` are thin wrappers over this rather than
   * three copies of the same auth-and-error block. They were two copies
   * until phase 7 needed a third verb, at which point a third copy would
   * have meant three places for a 401/403 distinction to drift apart --
   * and that distinction is load-bearing: the panel says "this key is not
   * an admin key" on one and "enter a key" on the other. */
  async _request(method, path, jsonBody) {
    const headers = {};
    const key = getApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;
    const init = { method, headers, signal: AbortSignal.timeout(20000) };
    if (jsonBody !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(jsonBody);
    }

    let res;
    try {
      res = await fetch(`${this.baseUrl}${path}`, init);
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

  async _get(path) {
    return this._request("GET", path);
  }

  async _post(path, jsonBody) {
    return this._request("POST", path, jsonBody ?? {});
  }

  async _patch(path, jsonBody) {
    return this._request("PATCH", path, jsonBody ?? {});
  }
}

async function _safeJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}
