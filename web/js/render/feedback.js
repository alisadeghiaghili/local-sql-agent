/* web/js/render/feedback.js — the analyst-facing "this answer was wrong"
 * control (admin panel phase 4, spec §2). Lives in web/, not web/admin/:
 * only the analyst who asked the question knows whether the answer was
 * wrong, and the flag control is theirs, not an admin surface.
 *
 * Deliberately small and low-key (spec §2.1): a single line of text below
 * the result, never a prominent button competing with the answer. One
 * interaction — pressing it reveals exactly one optional question (a
 * closed category set plus free text) and one submit action. A form that
 * takes a minute will not be filled in by someone who has already got
 * what they came for and moved on.
 *
 * Categories mirror appdb/feedback.py::FEEDBACK_CATEGORIES exactly —
 * both sides of this boundary must agree on the same closed set.
 */

"use strict";

const CATEGORIES = [
  { value: "wrong_number", label: "عدد اشتباه است" },
  { value: "different_question", label: "به سؤال دیگری پاسخ داده" },
  { value: "wrong_filter_or_period", label: "فیلتر یا بازهٔ زمانی اشتباه است" },
  { value: "other", label: "چیز دیگری" },
];

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

/**
 * Renders the flag control for one turn that produced a result.
 *
 * @param {{turn_id: string}} turn
 * @param {(category: string, note: string) => Promise<void>} onFlag —
 *   called with the chosen category and (possibly empty) note when the
 *   analyst submits. May reject (e.g. a network error, or "already
 *   flagged") — the control shows the rejection's message and lets the
 *   analyst try again rather than silently swallowing it.
 * @returns {HTMLElement}
 */
export function renderFeedbackControl(turn, onFlag) {
  const root = el("div", "feedback-control");
  root.dataset.turnId = turn.turn_id;

  const toggle = el("button", "feedback-toggle", "این عدد درست نیست؟");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  root.appendChild(toggle);

  const form = el("div", "feedback-form");
  form.hidden = true;
  root.appendChild(form);

  const select = document.createElement("select");
  select.className = "feedback-category";
  select.setAttribute("aria-label", "دستهٔ مشکل");
  for (const { value, label } of CATEGORIES) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
  form.appendChild(select);

  const note = document.createElement("textarea");
  note.className = "feedback-note";
  note.placeholder = "توضیح اختیاری";
  note.rows = 2;
  form.appendChild(note);

  const actions = el("div", "feedback-actions");
  const submit = el("button", "feedback-submit", "ارسال");
  submit.type = "button";
  actions.appendChild(submit);
  form.appendChild(actions);

  const status = el("div", "feedback-status");
  form.appendChild(status);

  toggle.addEventListener("click", () => {
    const willShow = form.hidden;
    form.hidden = !willShow;
    toggle.setAttribute("aria-expanded", String(willShow));
  });

  submit.addEventListener("click", async () => {
    submit.disabled = true;
    status.textContent = "";
    try {
      await onFlag(select.value, note.value.trim());
      status.textContent = "ثبت شد — سپاس از گزارش شما.";
      status.classList.add("ok");
      form.querySelectorAll("select, textarea, button").forEach((n) => { n.disabled = true; });
      toggle.textContent = "گزارش شد ✓";
    } catch (err) {
      status.textContent = (err && err.message) || "ثبت گزارش ناموفق بود.";
      status.classList.add("error");
      submit.disabled = false;
    }
  });

  /** Undo a submitted flag in the UI: the analyst confirmed the same
   * answer afterwards, and the server removed the flag (the later action
   * wins -- see render/confirmation.js). The control becomes usable again,
   * collapsed and empty, as if nothing had been submitted. */
  root.reopen = () => {
    form.querySelectorAll("select, textarea, button").forEach((n) => { n.disabled = false; });
    form.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "این عدد درست نیست؟";
    status.textContent = "";
    status.className = "feedback-status";
  };

  /** Expand the form and move focus into it -- what the «پاسخ نادرست است»
   * button on the answer does (render/turn.js), so the analyst need not
   * find this control in the drawer first. After a report was sent the
   * form is disabled; focus then goes to the toggle, whose text says so. */
  root.openForm = () => {
    form.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    const target = select.disabled ? toggle : select;
    if (typeof target.focus === "function") target.focus();
  };

  return root;
}
