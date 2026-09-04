/* web/js/render/turn.js — composes one full turn card, per the anatomy in
 * the brief: question -> resolved_question -> basis -> assumption chips ->
 * clarifications -> pipeline -> SQL -> result -> warnings -> llm status ->
 * interpretation.
 *
 * Built to tolerate partial/null Turn data throughout, because contract §6
 * requires the LLM block (and the rest of the card) to still render
 * whatever it has on failure — see data.js t_04 (guard-rejected, no
 * result) and t_05 (LLM transport failure, no sql/guard/result at all).
 */

"use strict";

import { renderPipeline } from "./pipeline.js";
import { renderBasis, renderAssumptions, renderClarifications } from "./assumptions.js";
import { renderResult, renderWarnings } from "./table.js";
import { renderLlmStatus, answerWasTruncated, renderTruncationQualifier } from "./llm-status.js";

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

  // 2. Resolved question.
  if (turn.resolved_question) {
    const card = el("div", "resolved-card");
    const label = el("div", "resolved-label");
    label.innerHTML = `<span aria-hidden="true">🧭</span> برداشت سامانه از پرسش`;
    const text = el("div", "resolved-text", turn.resolved_question);
    card.appendChild(label);
    card.appendChild(text);
    body.appendChild(tagEarly(card));
  }

  // 3. Basis indicator.
  const basisRow = renderBasis(turn.basis, ctx.onJumpToTurn);
  if (basisRow) body.appendChild(tagEarly(basisRow));

  // 4. Assumption chips.
  const assumptions = renderAssumptions(
    turn.ambiguity && turn.ambiguity.assumptions,
    (field, value) => ctx.onEditAssumption(turn.turn_id, field, value),
  );
  if (assumptions) body.appendChild(tagEarly(assumptions));

  // 5. Clarification offers.
  const clarifications = renderClarifications(
    turn.ambiguity && turn.ambiguity.clarifications,
    (field, option) => ctx.onClarify(turn.turn_id, field, option),
  );
  if (clarifications) body.appendChild(tagEarly(clarifications));

  // 6. Pipeline.
  const pipelineCard = el("div", "card pipeline-card");
  pipelineCard.appendChild(el("div", "card-title", "مراحل پردازش"));
  const pipeline = renderPipeline();
  pipelineCard.appendChild(pipeline.el);
  body.appendChild(pipelineCard);

  // Error banner (if the turn failed outright — e.g. LLM transport error).
  if (turn.error) {
    const banner = el("div", "error-banner");
    banner.setAttribute("role", "alert");
    const code = el("span", "error-code", turn.error.code);
    banner.appendChild(code);
    banner.appendChild(document.createTextNode(" " + turn.error.message));
    body.appendChild(tagLate(banner));
  }

  // 7. SQL.
  const sqlSection = el("div", "card sql-card-inner");
  if (turn.sql) {
    const titleRow = el("div", "card-title-row");
    titleRow.appendChild(el("span", "card-title", "SQL تولیدشده"));
    const copyBtn = el("button", "btn-copy", "کپی");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", () => copyToClipboard(turn.sql_display || turn.sql, copyBtn));
    titleRow.appendChild(copyBtn);
    sqlSection.appendChild(titleRow);

    const pre = document.createElement("pre");
    pre.className = "sql-box";
    pre.dir = "ltr";
    const code = document.createElement("code");
    code.textContent = turn.sql_display || turn.sql;
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
    body.appendChild(tagLate(sqlSection));
  } else if (!turn.error) {
    // No SQL and no error — unexpected but render honestly rather than
    // silently omitting the section.
    sqlSection.appendChild(el("div", "empty-result", "SQL تولید نشد."));
    body.appendChild(tagLate(sqlSection));
  }

  // 8. Result + row count/truncated + 9. warnings.
  if (!turn.error) {
    const resultCard = el("div", "card");
    resultCard.appendChild(el("div", "card-title", "نتیجه"));
    resultCard.appendChild(renderResult(turn.result, {
      assumptions: turn.ambiguity && turn.ambiguity.assumptions,
      guardRejected: !!(turn.guard && turn.guard.verdict === "rejected"),
    }));
    body.appendChild(tagLate(resultCard));
  }

  const warningsEl = renderWarnings(turn.warnings);
  if (warningsEl) body.appendChild(tagLate(warningsEl));

  // 10. LLM status strip.
  const llmCard = el("div", "card");
  llmCard.appendChild(renderLlmStatus(turn.llm));
  body.appendChild(tagLate(llmCard));

  // 11. Interpretation. Truncation (finish_reason: "length") qualifies the
  // WHOLE answer, so it renders above the interpretation text, read first,
  // in the status warning colour — not a footnote below it.
  if (turn.interpretation) {
    const interpCard = el("div", "card");
    if (answerWasTruncated(turn.llm)) interpCard.appendChild(renderTruncationQualifier());
    interpCard.appendChild(el("div", "card-title", "تفسیر"));
    interpCard.appendChild(el("p", "interpretation-text", turn.interpretation));
    body.appendChild(tagLate(interpCard));
  }

  collapseBtn.addEventListener("click", () => {
    const collapsed = root.classList.toggle("collapsed");
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
    collapseBtn.textContent = collapsed ? "باز کردن ▾" : "جمع کردن ▴";
  });

  function revealEarly() { earlyEls.forEach((n) => { n.hidden = false; }); }
  function revealLate() { lateEls.forEach((n) => { n.hidden = false; }); }
  if (!progressive) { revealEarly(); revealLate(); }

  return { el: root, pipeline, revealEarly, revealLate };
}

function summarize(turn) {
  const parts = [`#${turn.index}`];
  if (turn.basis && turn.basis.kind === "refines") parts.push(`ادامهٔ ${turn.basis.refines_turn_id}`);
  if (turn.error) parts.push(`خطا: ${turn.error.code}`);
  else if (turn.guard && turn.guard.verdict === "rejected") parts.push("رد شده توسط نگهبان امنیتی");
  else if (turn.result) parts.push(`${turn.result.row_count.toLocaleString("fa-IR")} ردیف`);
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
