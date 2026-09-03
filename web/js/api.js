/* web/js/api.js
 *
 * Live-mode transport against the real backend, per docs/api-contract-v2.md.
 * As of this writing the backend does NOT implement /v2/* yet (only /query
 * and /health exist — see api/server.py, api/models.py). Every function
 * here is written against the frozen contract so it is ready the moment the
 * backend lands, but callers MUST treat a 404 on /v2/sessions as "backend
 * doesn't support v2 yet", not as a generic error — see main.js.
 *
 * No fabricated results: on any failure we throw or return a tagged error
 * object; we never synthesize a fake Turn to paper over an unreachable API.
 *
 * Authentication (Phase 8, docs/api-contract-v2.md §11): every route here
 * except GET /health requires `Authorization: Bearer <key>`. The key comes
 * from apikey.js (one per analyst, stored in that analyst's own browser —
 * see that module's docstring for why a single baked-in key is wrong for
 * this UI) and is attached in exactly one place, `_fetchV2`, so every v2
 * call site inherits it automatically. `health()` attaches it too when one
 * is already stored (it unlocks the `model` field — see api/server.py's
 * `/health` handler) but never requires it: liveness probes must keep
 * working with no credentials at all.
 */

"use strict";

import { getApiKey } from "./apikey.js";

/* ── Contract types (JSDoc only, no runtime effect) ───────────────────
 * Mirrors docs/api-contract-v2.md §4 and §6 so editors can typecheck
 * render code against the frozen shape without a build step.
 *
 * @typedef {Object} Assumption
 * @property {string} field
 * @property {string} value
 * @property {"question"|"session"|"default"|"policy"} source
 * @property {boolean} editable
 *
 * @typedef {Object} Clarification
 * @property {string} field
 * @property {string} prompt
 * @property {string[]} options
 *
 * @typedef {Object} Ambiguity
 * @property {boolean} is_ambiguous
 * @property {Assumption[]} assumptions
 * @property {Clarification[]} clarifications
 *
 * @typedef {Object} Basis
 * @property {"fresh"|"refines"} kind
 * @property {string|null} refines_turn_id
 * @property {"cte"|"none"} composition
 * @property {string[]} inherited
 *
 * @typedef {Object} Guard
 * @property {"allowed"|"rejected"} verdict
 * @property {string|null} rule
 * @property {number|null} injected_top
 * @property {string[]} tables_touched
 *
 * @typedef {Object} ResultColumn
 * @property {string} name
 * @property {string} type
 *
 * @typedef {Object} Result
 * @property {ResultColumn[]} columns
 * @property {Object[]} rows
 * @property {number} row_count
 * @property {boolean} truncated
 *
 * @typedef {Object} LlmStatus
 * @property {string} backend
 * @property {string} model
 * @property {string|null} endpoint
 * @property {boolean} trusted
 * @property {number} endpoint_status
 * @property {number} attempts
 * @property {"stop"|"length"|"content_filter"|"tool_calls"|"schema_violation"|"error"|string} finish_reason
 *   -- literal values per docs/api-contract-v2.md §6, or an "other:<raw>"
 *   passthrough for a value none of them anticipated.
 * @property {boolean} structured_output
 * @property {number} prompt_tokens
 * @property {number} completion_tokens
 * @property {number|null} prefill_ms
 * @property {number|null} decode_ms
 * @property {number|null} total_ms
 * @property {number|null} tokens_per_second
 * @property {boolean} prefix_cache_hit
 * @property {number} temperature
 * @property {number|null} seed
 * @property {boolean|null} seed_honored
 * @property {number} corrections
 * @property {string} provider
 * @property {boolean} fallback_used
 * @property {boolean} reasoning_detected
 *
 * @typedef {Object} Turn
 * @property {string} turn_id
 * @property {string} session_id
 * @property {number} index
 * @property {string} question
 * @property {string|null} resolved_question
 * @property {Basis} basis
 * @property {string|null} sql
 * @property {string} [sql_display]
 * @property {Ambiguity} ambiguity
 * @property {Guard|null} guard
 * @property {Result|null} result
 * @property {string|null} interpretation
 * @property {"T0"|"T1"|"T2"|"T3"|null} tier
 * @property {string[]} warnings
 * @property {LlmStatus} llm
 * @property {Object} timings
 * @property {{code: string, message: string}|null} error
 */

export class V2NotSupportedError extends Error {
  constructor(message) {
    super(message);
    this.name = "V2NotSupportedError";
  }
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** A 401: missing or invalid API key. Distinguished from a generic
 * ApiError so the UI can show "your key was rejected, enter a new one"
 * and re-prompt instead of a generic error banner (docs/api-contract-v2.md
 * §11.3 — the server returns the same UNAUTHENTICATED code whether the
 * key was never sent or was sent and rejected, so this UI does not try to
 * tell those two cases apart either). */
export class UnauthorizedError extends ApiError {
  constructor(message) {
    super(message, 401);
    this.name = "UnauthorizedError";
  }
}

/** A 429: client-side rate limiting, not a query or model failure — see
 * api/middleware.py's RateLimitMiddleware. Carries the structured fields
 * from the error body (`retry_after_seconds`) so the UI can surface a
 * useful, specific message instead of a generic error. */
export class RateLimitError extends ApiError {
  constructor(message, retryAfterSeconds) {
    super(message, 429);
    this.name = "RateLimitError";
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export class Api {
  /** @param {string} baseUrl */
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  /* ── Health ──────────────────────────────────────────────────────── */
  /* Reads the REAL field names from api/models.py::HealthResponse
   * (status, openai, database, model). This drifted once already: an
   * earlier revision of this file read `h.openai` when the backend's
   * field was actually named `ollama`, then the backend was refactored
   * to a single OpenAI-compatible provider (commit 59dbc77) and the
   * field went back to `openai` — but this file had since been "fixed"
   * to read `h.ollama` against that now-superseded name, so it silently
   * went stale again and the LLM pill was permanently wrong in live mode
   * a second time. Reads `h.openai` here, matching the field that
   * actually exists on the response today. */
  async health(timeoutMs = 4000) {
    // Unauthenticated by design (docs/api-contract-v2.md §11.3 — GET
    // /health is the one route that stays open with no key at all, so a
    // liveness probe never depends on auth being configured correctly).
    // A key IS attached when one happens to be stored, purely so an
    // already-authenticated analyst sees the real `model` field instead
    // of it being omitted for "anonymous" callers (api/server.py's
    // `/health` handler) — its absence must never break this call.
    const headers = {};
    const key = getApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;
    const res = await fetch(`${this.baseUrl}/health`, { signal: AbortSignal.timeout(timeoutMs), headers });
    if (!res.ok) throw new ApiError(`HTTP ${res.status}`, res.status);
    const h = await res.json();
    return {
      api: true,
      llm: !!h.openai,
      db: !!h.database,
      status: h.status,
      model: h.model || null,
    };
  }

  /* ── Sessions (v2, not yet implemented server-side) ────────────────── */
  async createSession() {
    const res = await this._fetchV2(`/v2/sessions`, { method: "POST" });
    return res.json();
  }

  async getSession(sessionId) {
    const res = await this._fetchV2(`/v2/sessions/${encodeURIComponent(sessionId)}`, { method: "GET" });
    return res.json();
  }

  async deleteSession(sessionId) {
    await this._fetchV2(`/v2/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  }

  /**
   * Ask a non-streaming turn. Returns a Turn (contract §4).
   */
  async askTurn(sessionId, question) {
    const res = await this._fetchV2(`/v2/sessions/${encodeURIComponent(sessionId)}/turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    return res.json();
  }

  /**
   * Ask a streaming turn (contract §7). Calls `onEvent(eventName, payload)`
   * for every SSE frame in order: stage, resolved, assumptions, sql_delta,
   * sql, rows, interpretation_delta, llm, done, error.
   *
   * fetch()+ReadableStream is used instead of EventSource because this is a
   * POST with a body, which EventSource cannot express.
   */
  async askTurnStreaming(sessionId, question, onEvent, { signal } = {}) {
    const res = await this._fetchV2(
      `/v2/sessions/${encodeURIComponent(sessionId)}/turns?stream=1`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question }),
        signal,
      },
    );
    if (!res.body) throw new ApiError("Streaming response has no body (browser or server issue).", res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex;
      // SSE frames are separated by a blank line ("\n\n").
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const parsed = parseSseFrame(frame);
        if (parsed) onEvent(parsed.event, parsed.data);
      }
    }
  }

  async patchAssumptions(sessionId, turnId, assumptions) {
    const res = await this._fetchV2(
      `/v2/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/assumptions`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assumptions }),
      },
    );
    return res.json();
  }

  /** Wraps fetch for every /v2/* endpoint. All of these routes require
   * auth (docs/api-contract-v2.md §11.3), so this is the single place
   * that attaches `Authorization: Bearer <key>` — every v2 call site
   * (createSession, getSession, deleteSession, askTurn, askTurnStreaming,
   * patchAssumptions) goes through here and inherits it automatically.
   *
   * A 404 is promoted to V2NotSupportedError so callers can degrade to
   * simulated mode instead of showing a generic error. A 401 and a 429
   * are promoted to their own typed errors (see above) so the UI can
   * treat "your key was rejected" and "you're being rate-limited" as the
   * distinct, actionable situations they are instead of a generic error
   * banner. */
  async _fetchV2(path, init) {
    let res;
    const headers = { ...(init && init.headers) };
    const key = getApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        signal: AbortSignal.timeout(60000),
        ...init,
        headers,
      });
    } catch (err) {
      throw new ApiError(`Network error calling ${path}: ${err.message}`, 0);
    }
    if (res.status === 404) {
      throw new V2NotSupportedError(
        `${this.baseUrl} does not implement ${path} — the v2 conversational API is not deployed on this backend yet.`,
      );
    }
    if (res.status === 401) {
      const body = await _safeJson(res);
      throw new UnauthorizedError(body?.error?.message || body?.detail || "Missing or invalid API key.");
    }
    if (res.status === 429) {
      const body = await _safeJson(res);
      const message = body?.error?.message || body?.detail || `HTTP 429 calling ${path}`;
      const retryAfterSeconds = body?.error?.retry_after_seconds ?? null;
      throw new RateLimitError(message, retryAfterSeconds);
    }
    if (!res.ok) {
      const body = await _safeJson(res);
      // The real error envelope (api/errors.py::_error_response) nests
      // the message under `error.message`, not a top-level `detail` —
      // `j.detail` alone (the previous shape of this fallback) never
      // actually matched it, so every non-2xx response rendered as a
      // bare "HTTP <status>" regardless of what the server said.
      const detail = body?.error?.message || body?.detail || `HTTP ${res.status}`;
      throw new ApiError(detail, res.status);
    }
    return res;
  }
}

/** Best-effort JSON body read that never throws — an error response body
 * is not guaranteed to be valid JSON (or present at all). */
async function _safeJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

function parseSseFrame(frame) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;
  const raw = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}
