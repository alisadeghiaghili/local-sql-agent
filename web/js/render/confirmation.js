/* web/js/render/confirmation.js — the analyst-facing "this answer is
 * correct" control (پاسخ درست است), the positive twin of render/feedback.js.
 *
 * One toggle button, one interaction: pressing it confirms the answer,
 * pressing it again withdraws the confirmation. Each confirmation becomes
 * evidence — a reviewed golden-set candidate and a figure in the admin
 * panel's real-use quality card — so it has to cost one click and nothing
 * else: no form, no category, no free text.
 *
 * It appears only on a successful answer (canConfirm): the server refuses
 * a confirmation of anything else with a 409, and a button that can only
 * fail is worse than none.
 *
 * Accessibility: a real <button> (keyboard reachable, Enter/Space), with
 * `aria-pressed` carrying the state. The label stays the same word in both
 * states — the ARIA toggle-button pattern — and the visible state is a
 * check mark plus a border, not colour alone. Failures are written into a
 * role="status" line, so a screen reader hears them.
 *
 * Mirrors appdb/confirmations.py; the endpoint is
 * POST/DELETE /v2/sessions/{sid}/turns/{tid}/confirmation.
 */

"use strict";

const LABEL = "پاسخ درست است";
const CHECK = "✓";

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

/**
 * True when *turn* is an answer that can be confirmed: SQL ran and a
 * result came back (no error, not guard-rejected). Mirrors the server's
 * own rule (observability.audit.turn_executed_successfully) closely
 * enough that the button is never offered where the server would say 409.
 *
 * @param {object} turn
 * @returns {boolean}
 */
export function canConfirm(turn) {
  if (!turn || !turn.result || turn.error || !turn.sql) return false;
  if (turn.guard && turn.guard.verdict === "rejected") return false;
  return true;
}

/**
 * Renders the confirmation toggle for one answered turn.
 *
 * @param {{turn_id: string}} turn
 * @param {{
 *   confirmed?: boolean,
 *   onConfirm: () => Promise<unknown>,
 *   onWithdraw: () => Promise<unknown>,
 *   onConfirmed?: (result: unknown) => boolean | void,
 * }} handlers — `confirmed` is the initial state. `onConfirm` and
 *   `onWithdraw` perform the request; either may reject (a network
 *   error, a 409), in which case the control shows the message, keeps its
 *   previous state and stays usable. `onConfirmed` receives whatever
 *   `onConfirm` resolved with — the page uses it to learn that confirming
 *   also removed an open flag, and returns `true` when it did so the
 *   control can say that its earlier report was replaced.
 * @returns {HTMLElement} the control's root. It carries
 *   `setConfirmed(boolean)`, so the page can reflect a state change made
 *   elsewhere (flagging the same answer withdraws the confirmation).
 */
export function renderConfirmationControl(turn, handlers) {
  const root = el("div", "confirmation-control");
  root.dataset.turnId = turn.turn_id;

  const button = el("button", "confirm-toggle");
  button.type = "button";
  root.appendChild(button);

  const status = el("div", "confirmation-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  root.appendChild(status);

  let confirmed = false;

  function paint() {
    button.setAttribute("aria-pressed", String(confirmed));
    button.className = confirmed ? "confirm-toggle is-confirmed" : "confirm-toggle";
    button.textContent = confirmed ? `${CHECK} ${LABEL}` : LABEL;
  }

  function setConfirmed(value) {
    confirmed = Boolean(value);
    paint();
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "";
    status.className = "confirmation-status";
    const wasConfirmed = confirmed;
    try {
      let result;
      if (wasConfirmed) await handlers.onWithdraw();
      else result = await handlers.onConfirm();
      setConfirmed(!wasConfirmed);
      let replacedFlag = false;
      if (!wasConfirmed && handlers.onConfirmed) {
        // A page hook failing must not undo a confirmation the server stored.
        try { replacedFlag = handlers.onConfirmed(result) === true; } catch { replacedFlag = false; }
      }
      status.textContent = wasConfirmed
        ? "تأیید شما برداشته شد."
        : replacedFlag
          ? "ثبت شد — گزارش قبلی شما جایگزین شد."
          : "ثبت شد — سپاس از تأیید شما.";
      status.className = "confirmation-status ok";
      status.title = status.textContent;
    } catch (err) {
      setConfirmed(wasConfirmed);
      status.textContent = (err && err.message) || "ثبت تأیید ناموفق بود.";
      status.className = "confirmation-status error";
      status.title = status.textContent;  // the line is one row tall; the full text stays reachable
    } finally {
      button.disabled = false;
    }
  });

  setConfirmed(handlers.confirmed);
  root.setConfirmed = setConfirmed;
  return root;
}
