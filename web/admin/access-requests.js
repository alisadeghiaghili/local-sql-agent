/* web/admin/access-requests.js — the "Request access" queue render
 * (ADR-004 part 1, security only).
 *
 * A pure DOM-building module with no fetch/state of its own -- kept
 * separate from web/admin/main.js (which owns fetching and wiring, same
 * split main.js already keeps for renderKeys/renderVocabulary) so this
 * file is testable in isolation under Node, the same way
 * web/js/render/feedback.js is (see run_feedback_control.mjs): importing
 * main.js directly would also run its top-level bootstrap
 * (wireTopbar/refreshAll/etc.), which assumes a full page's worth of DOM
 * elements exist.
 *
 * Every value rendered here is untrusted server data -- a requester id,
 * a column name, the question joined from the audit log, a denial reason
 * -- and every one of them is set through `.textContent` (or a plain
 * DOM property, never a template string fed to `innerHTML`). The admin
 * panel had a stored XSS fixed in 4.12.1 (a principal id round-tripping,
 * quote intact, into a `data-*` attribute string -- see
 * web/admin/main.js's own `escapeHtml` comment); this module follows the
 * newer, structurally safer pattern `renderKeys`/`renderVocabulary`
 * already established there instead of that fix's `escapeHtml`-in-a-
 * string approach, so there is no string-built HTML here for a future
 * value to break out of in the first place.
 */

"use strict";

const STATUS_LABELS = { open: "باز", approved: "تأیید شده", denied: "رد شده" };
const STATUS_CLASS = { open: "skip", approved: "pass", denied: "fail" };

/**
 * Render the access-request queue into *host*, replacing its contents.
 *
 * @param {HTMLElement} host
 * @param {Array<{
 *   request_id: number, requester_principal_id: string, column_name: string,
 *   status: "open"|"approved"|"denied", created_at: string,
 *   resolution_note: string|null,
 *   audit: {question: string|null}|null,
 * }>} rows -- GET /admin/access-requests's `access_requests` list
 * @param {{
 *   onApprove: (requestId: number, btn: HTMLButtonElement) => void,
 *   onDeny: (requestId: number, reason: string, btn: HTMLButtonElement) => void,
 * }} handlers
 */
export function renderAccessRequestsList(host, rows, handlers) {
  host.innerHTML = "";
  if (!rows || !rows.length) {
    const empty = document.createElement("p");
    empty.className = "admin-loading";
    empty.textContent = "درخواستی یافت نشد.";
    host.appendChild(empty);
    return;
  }
  for (const row of rows) {
    host.appendChild(renderRow(row, handlers));
  }
}

function renderRow(row, handlers) {
  const cls = STATUS_CLASS[row.status] || "";
  const wrap = document.createElement("div");
  wrap.className = `admin-check ${cls}`;
  wrap.dataset.requestId = String(row.request_id);

  const mark = document.createElement("span");
  mark.className = "admin-check-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = row.status === "approved" ? "✓" : row.status === "denied" ? "✕" : "●";
  wrap.appendChild(mark);

  const body = document.createElement("span");

  const nameLine = document.createElement("span");
  nameLine.className = "admin-check-name";
  // Built from separate text nodes/elements, never a single interpolated
  // string -- requester_principal_id and column_name are both untrusted.
  nameLine.appendChild(document.createTextNode(`#${row.request_id} · `));
  nameLine.appendChild(document.createTextNode(row.requester_principal_id));
  nameLine.appendChild(document.createTextNode(" · "));
  const columnCode = document.createElement("code");
  columnCode.textContent = row.column_name;
  nameLine.appendChild(columnCode);
  body.appendChild(nameLine);

  body.appendChild(document.createTextNode(" "));
  const statusPill = document.createElement("span");
  statusPill.className = `admin-status-pill admin-status-${cls}`;
  statusPill.style.marginInlineStart = "6px";
  statusPill.textContent = STATUS_LABELS[row.status] || row.status;
  body.appendChild(statusPill);

  const question = row.audit && row.audit.question;
  const questionLine = document.createElement("p");
  questionLine.className = "admin-check-detail";
  questionLine.textContent = question || "(دیگر قابل بازیابی از گزارش ممیزی نیست)";
  body.appendChild(questionLine);

  const createdLine = document.createElement("p");
  createdLine.className = "admin-check-detail";
  createdLine.setAttribute("dir", "ltr");
  createdLine.textContent = row.created_at || "";
  body.appendChild(createdLine);

  if (row.status === "denied" && row.resolution_note) {
    const reasonLine = document.createElement("p");
    reasonLine.className = "admin-check-detail";
    reasonLine.appendChild(document.createTextNode("دلیل رد: "));
    reasonLine.appendChild(document.createTextNode(row.resolution_note));
    body.appendChild(reasonLine);
  }

  if (row.status === "open") {
    body.appendChild(renderActions(row, handlers));
  }

  wrap.appendChild(body);
  return wrap;
}

function renderActions(row, handlers) {
  const actions = document.createElement("div");
  actions.className = "admin-feedback-actions";

  const approveBtn = document.createElement("button");
  approveBtn.className = "admin-btn-refresh";
  approveBtn.type = "button";
  approveBtn.textContent = "تأیید";
  approveBtn.addEventListener("click", () => handlers.onApprove(row.request_id, approveBtn));
  actions.appendChild(approveBtn);

  const reasonInput = document.createElement("input");
  reasonInput.type = "text";
  reasonInput.className = "fb-note";
  reasonInput.placeholder = "دلیل رد (الزامی)";
  actions.appendChild(reasonInput);

  const denyBtn = document.createElement("button");
  denyBtn.className = "admin-btn-refresh key-danger";
  denyBtn.type = "button";
  denyBtn.textContent = "رد کردن";
  denyBtn.addEventListener("click", () => handlers.onDeny(row.request_id, reasonInput.value, denyBtn));
  actions.appendChild(denyBtn);

  return actions;
}
