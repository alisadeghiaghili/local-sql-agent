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
 */

"use strict";

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
 * @property {number} endpoint_status
 * @property {number} attempts
 * @property {"stop"|"length"|"schema_violation"|"error"} finish_reason
 * @property {boolean} structured_output
 * @property {number} prompt_tokens
 * @property {number} completion_tokens
 * @property {number} prefill_ms
 * @property {number} decode_ms
 * @property {number} total_ms
 * @property {number} tokens_per_second
 * @property {boolean} prefix_cache_hit
 * @property {number} temperature
 * @property {number} seed
 * @property {number} corrections
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

export class Api {
  /** @param {string} baseUrl */
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  /* ── Health ──────────────────────────────────────────────────────── */
  /* Reads the REAL field names from api/models.py::HealthResponse
   * (status, ollama, database, model) — the old demo read `h.openai`,
   * which does not exist on this response and made the LLM pill
   * permanently wrong in live mode. Fixed here. */
  async health(timeoutMs = 4000) {
    const res = await fetch(`${this.baseUrl}/health`, { signal: AbortSignal.timeout(timeoutMs) });
    if (!res.ok) throw new ApiError(`HTTP ${res.status}`, res.status);
    const h = await res.json();
    return {
      api: true,
      llm: !!h.ollama,
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

  /** Wraps fetch for /v2/* endpoints: a 404 is promoted to
   * V2NotSupportedError so callers can degrade to simulated mode instead of
   * showing a generic error. */
  async _fetchV2(path, init) {
    let res;
    try {
      res = await fetch(`${this.baseUrl}${path}`, { signal: AbortSignal.timeout(60000), ...init });
    } catch (err) {
      throw new ApiError(`Network error calling ${path}: ${err.message}`, 0);
    }
    if (res.status === 404) {
      throw new V2NotSupportedError(
        `${this.baseUrl} does not implement ${path} — the v2 conversational API is not deployed on this backend yet.`,
      );
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch { /* keep status-only detail */ }
      throw new ApiError(detail, res.status);
    }
    return res;
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
