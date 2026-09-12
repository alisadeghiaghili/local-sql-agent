/* web/js/render/turn.js — one turn card.
 *
 * Anatomy (DESIGN.md §5.2, api-contract §5/§7 — trust surface stays
 * visible; DESIGN-INVARIANTS.md §8 — failure gets the same three-part
 * treatment as success; §9 — what the analyst opted into stays in this
 * list, never inside the collapsed section at the end):
 *
 *   question
 *   resolved_question          (when present)
 *   basis                      (when refines)
 *   assumption chips + clarifications
 *   stage strip                (slim; the rest is one item down)
 *   outcome line               (rows · guard · top)
 *   failure state              (what happened · why · a control to press)
 *   SQL                        (collapsible; expanded on first/failure)
 *   result
 *   interpretation             (when requested — opted into, so main flow)
 *   pipeline / LLM / warnings / feedback   (collapsed; opt in to expand)
 *
 * Partial/null Turn data is tolerated throughout (contract §6).
 * SQL display: web/js/sql-display.js. Copy: copySourceOfTruth(turn).
 */

"use strict";

import { fmt } from "../num.js";
import { copySourceOfTruth, displaySqlForTurn, highlightSql } from "../sql-display.js";

import { renderPipeline, renderStageStrip } from "./pipeline.js";
import { renderBasis, renderAssumptions, renderClarifications } from "./assumptions.js";
import { renderResult, renderWarnings } from "./table.js";
import { renderLlmStatus, answerWasTruncated, renderTruncationQualifier } from "./llm-status.js";
import { renderFeedbackControl } from "./feedback.js";

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

/**
 * @param {import("../api.js").Turn} turn
 * @param {{
 *   onJumpToTurn: (turnId: string) => void,
 *   onEditAssumption: (turnId: string, field: string, value: string) => void,
 *   onClarify: (turnId: string, field: string, option: string) => void,
 *   onPin?: (turnId: string, field: string, value: string) => void,
 *   onRerun?: (turnId: string) => void,
 *   onRephrase?: (turnId: string) => void,
 *   onAskWithoutColumn?: (turnId: string, column: string) => void,
 *   onFlag?: (turnId: string, category: string, note: string) => Promise<void>,
 *   progressive?: boolean,
 * }} ctx
 */
export function createTurnCard(turn, ctx) {
  const progressive = !!(ctx && ctx.progressive);
  const earlyEls = [];
  const lateEls = [];
  function tagEarly(node) { if (progressive) { node.hidden = true; earlyEls.push(node); } return node; }
  function tagLate(node) { if (progressive) { node.hidden = true; lateEls.push(node); } return node; }

  const root = el("article", "turn");
  root.id = `turn-${turn.turn_id}`;
  root.dataset.turnId = turn.turn_id;

  // 1. Question bubble.
  const qBubble = el("div", "turn-question");
  const qIndex = el("span", "turn-index", `پرسش ${turn.index}`);
  qBubble.appendChild(qIndex);
  qBubble.appendChild(document.createTextNode(turn.question));
  root.appendChild(qBubble);

  // Collapse control + one-line summary (shown only while collapsed).
  const collapseBtn = el("button", "turn-collapse-btn", "جمع کردن ▴");
  collapseBtn.type = "button";
  collapseBtn.setAttribute("aria-expanded", "true");
  root.appendChild(collapseBtn);

  const summaryLine = el("div", "turn-summary-line");
  summaryLine.textContent = summarize(turn);
  root.appendChild(summaryLine);

  const body = el("div", "turn-body");
  root.appendChild(body);

  // 2. Resolved question — trust surface, always in the main flow.
  if (turn.resolved_question) {
    const card = el("div", "resolved-card");
    const label = el("div", "resolved-label");
    label.textContent = "برداشت سامانه از پرسش";
    const text = el("div", "resolved-text", turn.resolved_question);
    card.appendChild(label);
    card.appendChild(text);
    body.appendChild(tagEarly(card));
  }

  // 3. Basis.
  const basisRow = renderBasis(turn.basis, ctx.onJumpToTurn);
  if (basisRow) body.appendChild(tagEarly(basisRow));

  // 4. Assumption chips + clarifications — contract §5/§7, above the result.
  const assumptions = renderAssumptions(
    turn.ambiguity && turn.ambiguity.assumptions,
    (field, value) => ctx.onEditAssumption(turn.turn_id, field, value),
    ctx.onPin ? (field, value) => ctx.onPin(turn.turn_id, field, value) : undefined,
  );
  if (assumptions) body.appendChild(tagEarly(assumptions));

  const clarifications = renderClarifications(
    turn.ambiguity && turn.ambiguity.clarifications,
    (field, option) => ctx.onClarify(turn.turn_id, field, option),
  );
  if (clarifications) body.appendChild(tagEarly(clarifications));

  // 5. Slim stage strip (progress while streaming; summary after).
  const strip = renderStageStrip();
  body.appendChild(strip.el);
  if (progressive) earlyEls.push(strip.el);

  // Full pipeline list lives in the collapsed section below; setStage drives both.
  const pipelineList = renderPipeline();
  function setStage(key, state) {
    strip.setStage(key, state);
    pipelineList.setStage(key, state);
  }

  // Failure state (DESIGN-INVARIANTS.md §8) — checked before the result
  // card below so a guard rejection, which sets neither `turn.error` nor
  // a populated `turn.result`, still gets its own rendering instead of
  // silently producing nothing here and a "0 rows" card there.
  const failure = renderFailureState(turn, ctx);
  if (failure) body.appendChild(tagLate(failure));

  // 6. Outcome line — always after a settled turn.
  const outcome = buildOutcomeLine(turn);
  if (outcome) body.appendChild(outcome);

  // 7. SQL — collapsible; expanded when there is no result yet or on failure.
  const sqlSection = el("div", "card sql-card-inner");
  if (turn.sql) {
    const titleRow = el("div", "card-title-row");
    const sqlTitle = el("span", "card-title");
    sqlTitle.textContent = turn.guard && turn.guard.verdict === "allowed"
      ? "SQL تولیدشده · گارد ✓"
      : turn.guard && turn.guard.verdict === "rejected"
        ? "SQL تولیدشده · گارد ✕"
        : "SQL تولیدشده";
    titleRow.appendChild(sqlTitle);

    const acts = el("div", "sql-title-acts");
    const copyBtn = el("button", "btn-copy", "کپی");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", () => copyToClipboard(copySourceOfTruth(turn), copyBtn));
    acts.appendChild(copyBtn);

    const toggleBtn = el("button", "btn-copy sql-collapse", "پنهان");
    toggleBtn.type = "button";
    toggleBtn.setAttribute("aria-expanded", "true");
    acts.appendChild(toggleBtn);
    titleRow.appendChild(acts);
    sqlSection.appendChild(titleRow);

    const pre = document.createElement("pre");
    pre.className = "sql-box";
    pre.dir = "ltr";
    const code = document.createElement("code");
    code.className = "language-sql";
    highlightSql(code, displaySqlForTurn(turn));
    pre.appendChild(code);
    sqlSection.appendChild(pre);

    const meta = el("div", "sql-meta");
    if (turn.guard) {
      const guardPill = el("span", `guard-pill ${turn.guard.verdict}`,
        turn.guard.verdict === "allowed" ? "✓ مجاز" : "✕ رد شد");
      meta.appendChild(guardPill);
      if (turn.guard.injected_top !== null && turn.guard.injected_top !== undefined) {
        meta.appendChild(el("span", "meta-tag", `TOP ${turn.guard.injected_top}`));
      }
      if (turn.guard.tables_touched && turn.guard.tables_touched.length) {
        meta.appendChild(el("span", "meta-tag", turn.guard.tables_touched.join(", ")));
      }
    }
    sqlSection.appendChild(meta);

    if (turn.guard && turn.guard.verdict === "rejected" && turn.guard.rule) {
      sqlSection.appendChild(el("div", "guard-rule", turn.guard.rule));
    }

    // Collapse after a successful result is already on screen (DESIGN §5.2).
    const startCollapsed = !!(turn.result && turn.guard && turn.guard.verdict === "allowed");
    if (startCollapsed) {
      pre.hidden = true;
      meta.hidden = true;
      toggleBtn.setAttribute("aria-expanded", "false");
      toggleBtn.textContent = "نمایش SQL";
    }
    toggleBtn.addEventListener("click", () => {
      const open = pre.hidden;
      pre.hidden = !open;
      meta.hidden = !open;
      toggleBtn.setAttribute("aria-expanded", String(open));
      toggleBtn.textContent = open ? "پنهان" : "نمایش SQL";
    });

    body.appendChild(tagLate(sqlSection));
  } else if (!turn.error) {
    // A guard rejection also has no `turn.sql` (session/engine.py never
    // attaches the rejected statement to the outcome), but "SQL تولید
    // نشد" ("no SQL was generated") would be false — a statement WAS
    // generated, it just isn't retained for display. Say that instead.
    const noSqlMessage = isGuardRejected(turn)
      ? "SQL تولیدشده توسط گارد رد شد و برای نمایش نگه‌داری نمی‌شود."
      : "SQL تولید نشد.";
    sqlSection.appendChild(el("div", "empty-result", noSqlMessage));
    body.appendChild(tagLate(sqlSection));
  }

  // 8. Result — skipped for a guard rejection. The failure state above
  // already says this query never ran; a card titled "نتیجه" ("result")
  // directly under it, even with rejection-specific wording, is the exact
  // "did not run" vs. "returned nothing" confusion §8 rules out.
  if (!turn.error && !isGuardRejected(turn)) {
    const resultCard = el("div", "card");
    resultCard.appendChild(el("div", "card-title", "نتیجه"));
    resultCard.appendChild(renderResult(turn.result, {
      assumptions: turn.ambiguity && turn.ambiguity.assumptions,
      guardRejected: false,
      onRerun: ctx.onRerun ? () => ctx.onRerun(turn.turn_id) : undefined,
    }));
    body.appendChild(tagLate(resultCard));
  }

  // 8.5 Interpretation — rendered here, in the main flow, never inside
  // the collapsed section below. The analyst opted into this explicitly
  // (the toggle's own label states its cost: up to twenty rows sent to
  // the model), so hiding it behind a click would contradict the very
  // request it answers. Progressive disclosure is for what the product
  // chose to show, never for what the analyst chose to ask for.
  if (turn.interpretation) {
    const interpCard = el("div", "card");
    if (answerWasTruncated(turn.llm)) interpCard.appendChild(renderTruncationQualifier());
    interpCard.appendChild(el("div", "card-title", "تفسیر"));
    interpCard.appendChild(el("p", "interpretation-text", turn.interpretation));
    body.appendChild(tagLate(interpCard));
  }

  // 9. Everything else settles behind one click: pipeline timings and the
  // LLM status strip are shown BY THE PRODUCT, never asked for, which is
  // what makes collapsing them fine — unlike the interpretation above.
  const drawer = el("details", "turn-details");
  const drawerSummary = el("summary", "turn-details-summary", "جزئیات — pipeline، مدل، بازخورد");
  drawer.appendChild(drawerSummary);
  const drawerBody = el("div", "turn-details-body");

  const pipeWrap = el("div", "card pipeline-card");
  pipeWrap.appendChild(el("div", "card-title", "مراحل پردازش"));
  pipeWrap.appendChild(pipelineList.el);
  drawerBody.appendChild(pipeWrap);

  const warningsEl = renderWarnings(turn.warnings);
  if (warningsEl) drawerBody.appendChild(warningsEl);

  const llmCard = el("div", "card");
  llmCard.appendChild(renderLlmStatus(turn.llm));
  drawerBody.appendChild(llmCard);

  if (turn.result && ctx.onFlag) {
    const flagWrap = el("div", "card");
    flagWrap.appendChild(renderFeedbackControl(turn, (category, note) => ctx.onFlag(turn.turn_id, category, note)));
    drawerBody.appendChild(flagWrap);
  }

  drawer.appendChild(drawerBody);
  body.appendChild(tagLate(drawer));

  collapseBtn.addEventListener("click", () => {
    const collapsed = root.classList.toggle("collapsed");
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
    collapseBtn.textContent = collapsed ? "باز کردن ▾" : "جمع کردن ▴";
  });

  function revealEarly() { earlyEls.forEach((n) => { n.hidden = false; }); }
  function revealLate() { lateEls.forEach((n) => { n.hidden = false; }); }
  if (!progressive) { revealEarly(); revealLate(); }

  return {
    el: root,
    pipeline: { setStage, el: pipelineList.el, steps: pipelineList.steps },
    revealEarly,
    revealLate,
  };
}

/** True when the guard refused this turn's SQL before it ever ran.
 *
 * Checked ahead of `turn.error` everywhere in this file that renders a
 * failure, because a rejection does not set `turn.error` at all on this
 * (SSE) path — `session/engine.py`'s `_GenOutcome` sites attach it as
 * `guard=GuardVerdict(verdict="rejected", ...)` together with
 * `result=TurnResult()`, an EMPTY result. Read by shape alone that is
 * indistinguishable from a query that ran and matched nothing, which is
 * the exact confusion DESIGN-INVARIANTS.md §8 calls out: "did not run"
 * and "returned nothing" must never look the same. */
function isGuardRejected(turn) {
  return !!(turn.guard && turn.guard.verdict === "rejected");
}

/**
 * Renders the §8 failure anatomy: what happened (in the analyst's terms,
 * not the system's), why when the reason is known, and a next action as
 * a control — never a sentence telling them to go do something. The
 * machine-readable code rides along, small, after the sentence, for the
 * operator correlating it against a log; it is never the whole message.
 *
 * Guard rejection is resolved first, via `isGuardRejected`, before this
 * function ever looks at `turn.error` — see that helper's docstring for
 * why the ordering matters. The same underlying
 * `security.sql_guard.PolicyRejection` that produces a rejected
 * `GuardVerdict` here reaches an HTTP client as the `FORBIDDEN_SQL` error
 * code on the non-streaming route (`api/errors.py`'s `ForbiddenSQLError`
 * — see e.g. `tests/test_correction_loop_policy_rejection.py`), so the
 * `FORBIDDEN_SQL` case below is kept for that shape too, even though
 * `session/engine.py` does not produce it on the turn stream this UI
 * consumes today.
 *
 * @param {import("../api.js").Turn} turn
 * @param {{
 *   onRerun?: (turnId: string) => void,
 *   onRephrase?: (turnId: string) => void,
 *   onAskWithoutColumn?: (turnId: string, column: string) => void,
 * }} ctx
 * @returns {HTMLElement|null}
 */
function renderFailureState(turn, ctx) {
  if (isGuardRejected(turn)) {
    // `reason`/`subject` (session/models.py::GuardVerdict, populated from
    // security.sql_guard's own typed rejection -- see that module's
    // docstring) exist so THIS branch never has to regex `turn.guard.rule`
    // (free text kept verbatim for the audit trail and its own tests) to
    // find the column name a denied-column refusal is about. Without
    // them the only honest option was one generic action for every guard
    // rejection, which is what shipped before this field existed.
    const { reason, subject } = turn.guard;
    const lead = reason === "denied_column"
      ? "این پرسش اجرا نشد — یکی از ستون‌های لازم برای پاسخ به آن برای حساب شما محدود شده است. این به معنای «نتیجه‌ای یافت نشد» نیست."
      : "این پرسش اصلاً اجرا نشد — لایهٔ نگهبانی امنیتی پیش از اجرا آن را رد کرد.";

    const actions = [];
    // The targeted action DESIGN-INVARIANTS.md §8's table names for this
    // row ("Ask without that column") -- offered only when there is one
    // specific column to name (`subject`); a `*` that could expose more
    // than one denied column at once carries no single `subject` (see
    // security.sql_guard.validate_sql's star-expansion raise sites), so
    // there is nothing honest to put in this button's label there.
    if (reason === "denied_column" && subject && ctx.onAskWithoutColumn) {
      actions.push([
        `پرسش بدون «${subject}»`,
        () => ctx.onAskWithoutColumn(turn.turn_id, subject),
      ]);
    }
    // The generic action stays as a fallback for every OTHER guard
    // rejection (forbidden statement, an unresolvable `*`, ...) and as a
    // second, less specific option even when the targeted one above is
    // offered -- an analyst may want to change more than just drop one
    // column.
    if (ctx.onRephrase) {
      actions.push(["ویرایش پرسش", () => ctx.onRephrase(turn.turn_id)]);
    }

    return buildFailureBanner({
      severity: "crit",
      lead,
      why: turn.guard.rule,
      code: "FORBIDDEN_SQL",
      // A guard rejection never populates `turn.error` on this (SSE) path
      // (see `isGuardRejected`'s own docstring) -- `request_id` lives on
      // `TurnErrorInfo`, not `GuardVerdict`, so there is genuinely none to
      // show here, not one this file forgot to read.
      requestId: null,
      actions,
    });
  }

  if (!turn.error) return null;

  const retry = ctx.onRerun ? () => ctx.onRerun(turn.turn_id) : null;
  const requestId = turn.error.request_id || null;

  switch (turn.error.code) {
    // Severity follows docs/design/mockups/completions.html's own two
    // examples for these exact codes: a system hiccup an automatic retry
    // can plausibly fix reads as "warn" (amber); a statement that never
    // ran or a database that actively refused one reads as "crit" (red)
    // — both are a harder stop than "try again in a moment".
    case "MODEL_UNAVAILABLE":
      return buildFailureBanner({
        severity: "warn",
        lead: "پرسش شما نگه داشته شد — سامانهٔ مدل در دسترس نبود. این یک مشکل سیستمی است، نه ایرادی در پرسش شما.",
        why: turn.error.message,
        code: turn.error.code,
        requestId,
        actions: retry ? [["تلاش دوباره", retry]] : [],
      });

    case "LLM_OUTPUT_TRUNCATED":
      return buildFailureBanner({
        severity: "warn",
        lead: "مدل پیش از تمام‌کردن تولید پرس‌وجو متوقف شد.",
        why: turn.error.message,
        code: turn.error.code,
        requestId,
        actions: retry ? [["دوباره با پرسش کوتاه‌تر", retry]] : [],
      });

    // Kept for the HTTP shape this same rejection can take elsewhere —
    // see this function's docstring. `session/engine.py` never sets this
    // as `turn.error.code` today; `isGuardRejected` above is what fires
    // in practice for the turn stream this file renders.
    case "FORBIDDEN_SQL":
      return buildFailureBanner({
        severity: "crit",
        lead: "این پرسش اصلاً اجرا نشد — لایهٔ نگهبانی امنیتی پیش از اجرا آن را رد کرد.",
        why: turn.error.message,
        code: turn.error.code,
        requestId,
        actions: ctx.onRephrase
          ? [["ویرایش پرسش", () => ctx.onRephrase(turn.turn_id)]]
          : [],
      });

    case "QUERY_EXECUTION_ERROR":
      return buildFailureBanner({
        severity: "crit",
        lead: "پرس‌وجو اجرا شد، اما پایگاه داده آن را با خطا رد کرد.",
        why: turn.error.message,
        code: turn.error.code,
        requestId,
        actions: retry ? [["تلاش دوباره", retry]] : [],
      });

    default:
      // Every other code (MODEL_TIMEOUT, DATABASE_UNAVAILABLE, ...) still
      // gets the anatomy — message as "what happened", code subordinate,
      // and a real retry control — just not a code-specific sentence.
      return buildFailureBanner({
        severity: "crit",
        lead: turn.error.message,
        why: null,
        code: turn.error.code,
        requestId,
        actions: retry ? [["تلاش دوباره", retry]] : [],
      });
  }
}

/**
 * One failure banner, styled after docs/design/mockups/completions.html's
 * `.fail` pattern: a left (logical-start) accent border in the severity
 * colour, a bold lead sentence, an optional lighter "why", the
 * machine-readable code (and, when there is one, the `request_id` beside
 * it) as a small monospace line — present but visually the least
 * important thing here, per DESIGN-INVARIANTS.md §8's copy discipline
 * ("Keep the machine-readable code and the request_id visible but
 * subordinate — an operator needs them; the sentence is not for them") —
 * and real `<button>` controls, the first one filled in brand teal (the
 * actual next step), any further one a plain outline (an alternative, not
 * the recommended path). `actions` is a list of `[label, onClick]` pairs
 * rather than objects, matching this file's `el(tag, className, text)`
 * helper's own positional style.
 *
 * @param {{
 *   severity: "crit"|"warn",
 *   lead: string,
 *   why?: string|null,
 *   code?: string|null,
 *   requestId?: string|null,
 *   actions: [string, () => void][],
 * }} spec
 */
function buildFailureBanner({ severity, lead, why, code, requestId, actions }) {
  const banner = el("div", `failure-state failure-${severity}`);
  banner.setAttribute("role", "alert");

  banner.appendChild(el("p", "failure-lead", lead));

  if (why && why !== lead) {
    banner.appendChild(el("p", "failure-why", why));
  }

  if (code || requestId) {
    const meta = el("p", "failure-meta");
    if (code) meta.appendChild(el("span", "failure-code", code));
    // `requestId` is server-supplied text (session/models.py's
    // TurnErrorInfo.request_id) rendered with `el()`'s `textContent`
    // assignment, same as every other field on this page — never
    // interpolated into an HTML string (DESIGN-INVARIANTS.md §1.3).
    if (requestId) meta.appendChild(el("span", "failure-request-id", `req: ${requestId}`));
    banner.appendChild(meta);
  }

  if (actions && actions.length) {
    const actionsRow = el("div", "failure-actions");
    actions.forEach(([label, onClick], i) => {
      const btn = el("button", i === 0 ? "failure-action-btn primary" : "failure-action-btn", label);
      btn.type = "button";
      btn.addEventListener("click", onClick);
      actionsRow.appendChild(btn);
    });
    banner.appendChild(actionsRow);
  }

  return banner;
}

/** One-line outcome: rows · guard · TOP. Always after a settled turn. */
function buildOutcomeLine(turn) {
  if (turn.error && !turn.guard && !turn.result) return null;
  const row = el("div", "turn-outcome");
  if (turn.guard) {
    const ok = turn.guard.verdict === "allowed";
    row.appendChild(el("span", ok ? "outcome-ok" : "outcome-bad",
      ok ? "✓ گارد مجاز" : "✕ گارد رد شد"));
  }
  if (turn.result && turn.result.row_count !== undefined && turn.result.row_count !== null) {
    row.appendChild(el("span", "outcome-sep", "·"));
    row.appendChild(el("span", "", `${fmt(turn.result.row_count)} ردیف`));
  }
  if (turn.guard && turn.guard.injected_top !== null && turn.guard.injected_top !== undefined) {
    row.appendChild(el("span", "outcome-sep", "·"));
    row.appendChild(el("span", "num", `TOP ${turn.guard.injected_top}`));
  }
  if (turn.timings && turn.timings.total_ms) {
    row.appendChild(el("span", "outcome-sep", "·"));
    const secs = turn.timings.total_ms / 1000;
    row.appendChild(el("span", "num", `${secs.toFixed(1)}s`));
  }
  if (turn.ambiguity && turn.ambiguity.is_ambiguous) {
    row.appendChild(el("span", "outcome-sep", "·"));
    row.appendChild(el("span", "outcome-warn", "مبهم — با مفروضات"));
  }
  if (!row.childNodes.length) return null;
  return row;
}

function summarize(turn) {
  const parts = [`#${turn.index}`];
  if (turn.basis && turn.basis.kind === "refines") parts.push(`ادامهٔ ${turn.basis.refines_turn_id}`);
  if (turn.error) parts.push(`خطا: ${turn.error.code}`);
  else if (turn.guard && turn.guard.verdict === "rejected") parts.push("رد شده توسط نگهبان امنیتی");
  else if (turn.result) parts.push(`${fmt(turn.result.row_count)} ردیف`);
  if (turn.ambiguity && turn.ambiguity.is_ambiguous) parts.push("مبهم — با مفروضات پاسخ داده شد");
  return parts.join(" · ");
}

async function copyToClipboard(text, btn) {
  const originalLabel = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    } catch { /* clipboard unavailable — button label stays "کپی" */ }
  }
  btn.textContent = "کپی شد ✓";
  setTimeout(() => { btn.textContent = originalLabel; }, 1600);
}
