/* web/js/render/assumptions.js — basis indicator, assumption chips, and
 * clarification offers (contract §4, §5).
 *
 * Chips are colour-coded by `source` (question / session / default /
 * policy) so the UI shows, at a glance, which follow-ups are inheriting
 * session context. `policy` chips are never editable (§5) and are styled
 * non-interactive on purpose. Editable chips open an inline control that
 * calls back into `onEditAssumption` — the caller (main.js) decides what
 * that means for the current mode (PATCH in live mode, local re-run in
 * simulated mode).
 *
 * Clarifications render as one-click buttons, never a blocking modal — the
 * contract is explicit that the turn already has an answer; clarifying is
 * an offer to refine it, not a gate the user must pass first.
 */

"use strict";

const SOURCE_LABELS = {
  question: "از پرسش",
  session: "از نشست",
  default: "پیش‌فرض",
  policy: "قانون سامانه",
};

/**
 * @param {import("../api.js").Basis} basis
 * @param {(turnId: string) => void} onJumpToTurn
 */
export function renderBasis(basis, onJumpToTurn) {
  if (!basis || basis.kind !== "refines") return null;
  const row = document.createElement("div");
  row.className = "basis-row";

  const label = document.createElement("span");
  label.textContent = "ادامهٔ";
  row.appendChild(label);

  const link = document.createElement("button");
  link.type = "button";
  link.className = "basis-link";
  link.textContent = basis.refines_turn_id;
  link.addEventListener("click", () => onJumpToTurn(basis.refines_turn_id));
  row.appendChild(link);

  if (basis.inherited && basis.inherited.length) {
    const inhLabel = document.createElement("span");
    inhLabel.textContent = "— وراثت:";
    row.appendChild(inhLabel);
    for (const item of basis.inherited) {
      const chip = document.createElement("span");
      chip.className = "basis-chip";
      chip.textContent = item;
      row.appendChild(chip);
    }
  }
  return row;
}

/**
 * @param {import("../api.js").Assumption[]} assumptions
 * @param {(field: string, newValue: string) => void} onEditAssumption
 */
export function renderAssumptions(assumptions, onEditAssumption) {
  if (!assumptions || assumptions.length === 0) return null;
  const wrap = document.createElement("div");
  wrap.className = "assumptions";

  for (const a of assumptions) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.source = a.source;

    const field = document.createElement("span");
    field.className = "chip-field";
    field.textContent = a.field + ":";
    chip.appendChild(field);

    const value = document.createElement("span");
    value.className = "chip-value";
    value.textContent = a.value;
    chip.appendChild(value);

    const src = document.createElement("span");
    src.className = "chip-source";
    src.textContent = SOURCE_LABELS[a.source] || a.source;
    chip.appendChild(src);

    if (a.editable) {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "chip-edit-btn";
      editBtn.setAttribute("aria-label", `ویرایش مفروضهٔ ${a.field}`);
      editBtn.textContent = "✎";
      editBtn.addEventListener("click", () => openEditor(chip, a, onEditAssumption));
      chip.appendChild(editBtn);
    } else {
      const lock = document.createElement("span");
      lock.className = "chip-noneditable-mark";
      lock.setAttribute("aria-hidden", "true");
      lock.title = "این مفروضه توسط قانون سامانه تعیین شده و قابل‌ویرایش نیست";
      lock.textContent = "🔒";
      chip.appendChild(lock);
    }

    wrap.appendChild(chip);
  }
  return wrap;
}

function openEditor(chipEl, assumption, onEditAssumption) {
  const existing = chipEl.parentElement.querySelector(".chip-editor");
  if (existing) existing.remove();

  const editor = document.createElement("div");
  editor.className = "chip-editor";

  const input = document.createElement("input");
  input.type = "text";
  input.value = assumption.value;
  input.setAttribute("aria-label", `مقدار جدید برای ${assumption.field}`);

  const apply = document.createElement("button");
  apply.type = "button";
  apply.className = "btn-apply";
  apply.textContent = "اعمال";
  apply.addEventListener("click", () => {
    const val = input.value.trim();
    editor.remove();
    if (val && val !== assumption.value) onEditAssumption(assumption.field, val);
  });

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn-cancel";
  cancel.textContent = "لغو";
  cancel.addEventListener("click", () => editor.remove());

  editor.appendChild(input);
  editor.appendChild(apply);
  editor.appendChild(cancel);
  chipEl.insertAdjacentElement("afterend", editor);
  input.focus();
  input.select();
}

/**
 * @param {import("../api.js").Clarification[]} clarifications
 * @param {(field: string, option: string) => void} onClarify
 */
export function renderClarifications(clarifications, onClarify) {
  if (!clarifications || clarifications.length === 0) return null;
  const wrap = document.createElement("div");
  wrap.className = "clarifications";

  for (const c of clarifications) {
    const row = document.createElement("div");
    row.className = "clarify-row";
    const prompt = document.createElement("span");
    prompt.className = "clarify-prompt";
    prompt.textContent = c.prompt;
    row.appendChild(prompt);
    for (const opt of c.options) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarify-opt";
      btn.textContent = opt;
      btn.addEventListener("click", () => onClarify(c.field, opt));
      row.appendChild(btn);
    }
    wrap.appendChild(row);
  }
  return wrap;
}
