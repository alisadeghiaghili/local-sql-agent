/* web/js/render/sessions.js — the conversation rail: session list, rename
 * in place, and delete with a confirm that names what is being deleted.
 *
 * Per the spec: ordered by `last_active_at`, newest first, REGARDLESS of
 * the order the server (or the simulated index) returned — sorting happens
 * here, not trusted from the caller, so a server that ever changes its own
 * ordering can never silently break the rail.
 */

"use strict";

import { fmt } from "../num.js";

/** Newest-active-first. Pure, no DOM — returns a new array, never mutates
 * the input, so callers can safely reuse the same SessionSummary[] between
 * renders. */
export function sortSessionsByRecency(sessions) {
  return (sessions || [])
    .slice()
    .sort((a, b) => new Date(b.last_active_at).getTime() - new Date(a.last_active_at).getTime());
}

const RELATIVE_UNITS_FA = [
  ["سال", 365 * 24 * 3600],
  ["ماه", 30 * 24 * 3600],
  ["هفته", 7 * 24 * 3600],
  ["روز", 24 * 3600],
  ["ساعت", 3600],
  ["دقیقه", 60],
];

/** A relative-time label ("۳ روز پیش") for a row's `last_active_at`. Pure;
 * `now` is injectable for deterministic tests. */
export function relativeTimeFa(iso, now = new Date()) {
  const then = new Date(iso).getTime();
  const nowMs = now instanceof Date ? now.getTime() : new Date(now).getTime();
  const diffSec = Math.max(0, Math.round((nowMs - then) / 1000));
  if (diffSec < 60) return "چند لحظه پیش";
  for (const [label, secs] of RELATIVE_UNITS_FA) {
    if (diffSec >= secs) return `${fmt(Math.floor(diffSec / secs))} ${label} پیش`;
  }
  return "چند لحظه پیش";
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

function openRenameEditor(rowEl, session, onRename) {
  const existing = rowEl.querySelector(".session-row-editor");
  if (existing) { existing.remove(); return; }

  const editor = el("div", "session-row-editor");
  const input = document.createElement("input");
  input.type = "text";
  input.value = session.title || "";
  input.setAttribute("aria-label", `نام جدید برای گفتگوی «${session.title || session.session_id}»`);

  const apply = el("button", null, "اعمال");
  apply.type = "button";
  apply.addEventListener("click", () => {
    const val = input.value.trim();
    editor.remove();
    if (val && val !== session.title && onRename) onRename(session.session_id, val);
  });

  const cancel = el("button", null, "لغو");
  cancel.type = "button";
  cancel.addEventListener("click", () => editor.remove());

  editor.appendChild(input);
  editor.appendChild(apply);
  editor.appendChild(cancel);
  rowEl.appendChild(editor);
  input.focus();
  input.select();
}

/**
 * @param {import("../api.js").SessionSummary[]} sessions
 * @param {{
 *   selectedId?: string|null,
 *   onSelect?: (sessionId: string) => void,
 *   onRename?: (sessionId: string, title: string) => void,
 *   onDelete?: (sessionId: string) => void,
 *   confirmDelete?: (session: import("../api.js").SessionSummary) => boolean,
 * }} [handlers]
 */
export function renderSessionList(sessions, handlers = {}) {
  const { selectedId, onSelect, onRename, onDelete } = handlers;
  const confirmDelete = handlers.confirmDelete || ((session) =>
    (typeof window !== "undefined" && window.confirm)
      ? window.confirm(
          `گفتگوی «${session.title || session.session_id}» برای همیشه حذف شود؟ ` +
          "این کار غیرقابل‌بازگشت است و کل تاریخچهٔ این گفتگو از بین می‌رود.",
        )
      : true);

  const wrap = el("div", "session-list");

  const ordered = sortSessionsByRecency(sessions);
  if (ordered.length === 0) {
    const empty = el("div", "session-list-empty");
    empty.textContent = "هنوز هیچ گفتگویی وجود ندارد — یک پرسش بپرسید تا اولین گفتگو ساخته شود.";
    wrap.appendChild(empty);
    return wrap;
  }

  for (const s of ordered) {
    const row = el("div", "session-row" + (s.session_id === selectedId ? " active" : ""));
    row.dataset.sessionId = s.session_id;

    const main = document.createElement("button");
    main.type = "button";
    main.className = "session-row-main";
    main.setAttribute("aria-current", s.session_id === selectedId ? "true" : "false");
    main.addEventListener("click", () => onSelect && onSelect(s.session_id));

    main.appendChild(el("span", "session-row-title", s.title || "(بدون عنوان)"));
    const turnCount = typeof s.turn_count === "number" ? s.turn_count : 0;
    main.appendChild(el(
      "span", "session-row-meta",
      `${relativeTimeFa(s.last_active_at)} · ${fmt(turnCount)} نوبت`,
    ));
    row.appendChild(main);

    const actions = el("div", "session-row-actions");

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "session-row-btn";
    renameBtn.setAttribute("aria-label", `تغییر نام گفتگوی «${s.title || s.session_id}»`);
    renameBtn.textContent = "✎";
    renameBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openRenameEditor(row, s, onRename);
    });
    actions.appendChild(renameBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "session-row-btn session-row-delete";
    delBtn.setAttribute("aria-label", `حذف گفتگوی «${s.title || s.session_id}»`);
    delBtn.textContent = "🗑";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (confirmDelete(s) && onDelete) onDelete(s.session_id);
    });
    actions.appendChild(delBtn);

    row.appendChild(actions);
    wrap.appendChild(row);
  }
  return wrap;
}
