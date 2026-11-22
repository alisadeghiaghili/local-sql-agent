/* web/js/render/assumptions.js — basis indicator, assumption chips, and
 * clarification offers (contract §4, §5).
 *
 * Chips are colour-coded by `source` (question / session / default /
 * policy / memory) so the UI shows, at a glance, which follow-ups are
 * inheriting session context. `policy` chips are never editable (§5) and
 * are styled non-interactive on purpose. Editable chips open an inline
 * control that calls back into `onEditAssumption` — the caller (main.js)
 * decides what that means for the current mode (PATCH in live mode, local
 * re-run in simulated mode).
 *
 * `memory` is the fifth source: a standing preference the analyst pinned
 * explicitly (never inferred) is now being applied automatically. Every
 * editable, non-memory chip also carries a "pin this" control (📌) that
 * calls `onPin(field, value)` — this is the ONLY way memory gets created;
 * see web/js/render/memory.js and PUT /v2/memory/{key}.
 *
 * Clarifications render as an inline label + <select>, never a blocking
 * modal — the contract is explicit that the turn already has an answer;
 * clarifying is an offer to refine it, not a gate the user must pass
 * first. A select (not a button row) because the closed option set can
 * hold twenty-plus rings (e.g. the IME halls) and a wall of pills is
 * unreadable; the <option value> stays the backend string so onClarify
 * sends what the policy vocabulary matches on.
 */

"use strict";

import { icon } from "../icons.js";

export const SOURCE_LABELS = {
  question: "از پرسش",
  session: "از نشست",
  default: "پیش‌فرض",
  policy: "قانون سامانه",
  memory: "از حافظه",
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
 * @param {(field: string, value: string) => void} [onPin] — "pin this as a
 *   standing preference" (PUT /v2/memory/{key}). Omitted entirely (no pin
 *   button rendered at all) when the caller passes nothing, so existing
 *   call sites that don't yet support memory keep working unchanged.
 * @param {Set<string>} [clarifiedFields] — fields the same turn also
 *   surfaces as a clarification select. For those, the chip goes
 *   read-only (value + source tag, no select / edit / pin): the
 *   clarification is the single place to change the value, and two
 *   selects asking the same question at once just looked like a bug.
 *   Optional; undefined/empty changes nothing.
 */
export function renderAssumptions(assumptions, onEditAssumption, onPin, ringOptions, clarifiedFields) {
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

    // A field the same turn also offers as a clarification select is
    // shown READ-ONLY here (value + source tag): the clarification is
    // the single place to change it. Without this, a default-sourced
    // ring/measure produced two selects asking the same question —
    // one in the chip, one in the clarification — which read as a bug.
    const isClarified = clarifiedFields != null && clarifiedFields.has(a.field);

    // The `ring` (تالار) field, when its closed option set is known, is a
    // <select> — the same control as the memory panel's ring field — rather
    // than a text value + inline editor. Applying is on-change: picking a
    // ring is the only thing this control does, so there is no separate
    // apply/cancel step. Other fields (and a ring with no known options)
    // keep the existing text value + edit button below.
    const useRingSelect = !isClarified && a.editable && a.field === "ring"
      && Array.isArray(ringOptions) && ringOptions.length > 0;
    if (useRingSelect) {
      const select = document.createElement("select");
      select.className = "chip-select";
      select.setAttribute("aria-label", `تغییر «${a.field}»`);
      const opts = ringOptions.includes(a.value) ? ringOptions : [a.value, ...ringOptions];
      for (const opt of opts) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        o.selected = opt === a.value;
        select.appendChild(o);
      }
      select.addEventListener("change", () => {
        if (select.value !== a.value) onEditAssumption(a.field, select.value);
      });
      chip.appendChild(select);
    } else {
      const value = document.createElement("span");
      value.className = "chip-value";
      value.textContent = a.value;
      chip.appendChild(value);
    }

    const src = document.createElement("span");
    src.className = "chip-source";
    src.textContent = SOURCE_LABELS[a.source] || a.source;
    chip.appendChild(src);

    if (isClarified) {
      // No controls at all: the clarification select below is the
      // single place to change this value, an edit button would be a
      // second path to the same action, and pinning is withheld too —
      // a clarified value is still a DEFAULT guess the user has not
      // confirmed, and "pin this" must never be offered on a guess.
    } else if (a.editable) {
      // A ring chip already edits itself through the <select> above; a
      // separate edit button would be a redundant second path to the same
      // action. Other editable fields still get the inline text editor.
      if (!useRingSelect) {
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "chip-edit-btn";
        editBtn.setAttribute("aria-label", `ویرایش مفروضهٔ ${a.field}`);
        editBtn.appendChild(icon("edit", { size: 14 }));
        editBtn.addEventListener("click", () => openEditor(chip, a, onEditAssumption));
        chip.appendChild(editBtn);
      }

      // "Pin this" — the ONLY way memory gets created (never inferred).
      // Not offered on a chip that is ALREADY memory-sourced: re-pinning
      // its own current value is a no-op the analyst has no use for.
      if (onPin && a.source !== "memory") {
        const pinBtn = document.createElement("button");
        pinBtn.type = "button";
        pinBtn.className = "chip-pin-btn";
        pinBtn.setAttribute("aria-label", `به‌خاطر سپردن «${a.field}: ${a.value}» به‌عنوان اولویت ثابت`);
        pinBtn.title = "این مقدار را برای گفتگوهای بعدی هم به‌خاطر بسپار";
        pinBtn.appendChild(icon("pin", { size: 14 }));
        pinBtn.addEventListener("click", () => onPin(a.field, a.value));
        chip.appendChild(pinBtn);
      }
    } else {
      const lock = document.createElement("span");
      lock.className = "chip-noneditable-mark";
      lock.setAttribute("aria-hidden", "true");
      lock.title = "این مفروضه توسط قانون سامانه تعیین شده و قابل‌ویرایش نیست";
      lock.appendChild(icon("shield", { size: 14 }));
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

/* Clarification copy arrives from the backend in mixed languages: the
 * ring prompt and options are English (session_policy.yaml), the
 * measure prompt and options are Persian (session/ambiguity.py). Map
 * each canonical string to the active UI language — same read-`<html
 * lang>` convention as chart.js's `viewWord`, keeping this module's
 * standalone graph. The <option VALUE> must stay the original backend
 * string: `onClarify` sends what the policy/ambiguity vocabulary
 * matches on (e.g. "All rings", "ارزش ریالی معامله"), never the
 * translated display text. */
const CLARIFICATION_FA = {
  "Which ring do you mean?": "انتخاب تالار",
  "All rings": "همه تالارها",
};
const CLARIFICATION_EN = {
  "«برتر» بر اساس کدام معیار؟": "By which criterion is “top” ranked?",
  "ارزش ریالی معامله": "Rial trade value",
  "حجم معامله": "Trade volume",
  "تعداد قرارداد": "Contract count",
};
function uiClarLabel(text) {
  const lang = document.documentElement && document.documentElement.lang;
  const table = lang === "en" ? CLARIFICATION_EN : CLARIFICATION_FA;
  return table[text] || text;
}

/**
 * @param {import("../api.js").Clarification[]} clarifications
 * @param {(field: string, option: string) => void} onClarify
 */
export function renderClarifications(clarifications, onClarify) {
  if (!clarifications || clarifications.length === 0) return null;
  const wrap = document.createElement("div");
  wrap.className = "clarifications";

  for (const [i, c] of clarifications.entries()) {
    const row = document.createElement("div");
    row.className = "clarify-row";

    const label = document.createElement("label");
    label.className = "clarify-prompt";
    label.htmlFor = `clarify-select-${i}`;
    label.textContent = uiClarLabel(c.prompt);
    row.appendChild(label);

    const select = document.createElement("select");
    select.id = `clarify-select-${i}`;
    select.className = "clarify-select";
    for (const opt of c.options) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = uiClarLabel(opt);
      select.appendChild(o);
    }
    select.addEventListener("change", () => onClarify(c.field, select.value));
    row.appendChild(select);

    wrap.appendChild(row);
  }
  return wrap;
}
