/* web/js/render/turn.js — one turn card.
 *
 * Anatomy (DESIGN.md §5.2, api-contract §5/§7 — trust surface stays visible):
 *
 *   question
 *   resolved_question          (when present)
 *   basis                      (when refines)
 *   assumption chips + clarifications
 *   stage strip                (slim; full list in the drawer)
 *   outcome line               (rows · guard · top)
 *   SQL                        (collapsible; expanded on first/failure)
 *   result
 *   details drawer             (pipeline, LLM, warnings, interpretation, feedback)
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

  // 2. Resolved question — trust surface, never in the drawer.
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

  // Full pipeline list lives in the drawer; setStage drives both.
  const pipelineList = renderPipeline();
  function setStage(key, state) {
    strip.setStage(key, state);
    pipelineList.setStage(key, state);
  }

  // Error banner.
  if (turn.error) {
    const banner = el("div", "error-banner");
    banner.setAttribute("role", "alert");
    const code = el("span", "error-code", turn.error.code);
    banner.appendChild(code);
    banner.appendChild(document.createTextNode(" " + turn.error.message));
    body.appendChild(tagLate(banner));
  }

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
    sqlSection.appendChild(el("div", "empty-result", "SQL تولید نشد."));
    body.appendChild(tagLate(sqlSection));
  }

  // 8. Result.
  if (!turn.error) {
    const resultCard = el("div", "card");
    resultCard.appendChild(el("div", "card-title", "نتیجه"));
    resultCard.appendChild(renderResult(turn.result, {
      assumptions: turn.ambiguity && turn.ambiguity.assumptions,
      guardRejected: !!(turn.guard && turn.guard.verdict === "rejected"),
      onRerun: ctx.onRerun ? () => ctx.onRerun(turn.turn_id) : undefined,
    }));
    body.appendChild(tagLate(resultCard));
  }

  // 9. Details drawer — timings, LLM, warnings, interpretation, feedback.
  const drawer = el("details", "turn-details");
  const drawerSummary = el("summary", "turn-details-summary", "جزئیات — pipeline، مدل، تفسیر، بازخورد");
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

  if (turn.interpretation) {
    const interpCard = el("div", "card");
    if (answerWasTruncated(turn.llm)) interpCard.appendChild(renderTruncationQualifier());
    interpCard.appendChild(el("div", "card-title", "تفسیر"));
    interpCard.appendChild(el("p", "interpretation-text", turn.interpretation));
    drawerBody.appendChild(interpCard);
  }

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
