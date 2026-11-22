/* web/js/navigator.js — the vertical dot timeline beside the transcript.
 *
 * A conversation is a sequence of questions, each answered in place. Once the
 * transcript grows past a couple of turns, "which question am I on" stops
 * being obvious from the page itself. The navigator answers that: one dot per
 * turn, in order, with a running connector and each dot coloured by its own
 * outcome — the same status vocabulary `dashboard.js::turnStatus` paints on
 * the recent-queries rows, so an analyst reads "answered / no rows / failed /
 * still running" the same way everywhere.
 *
 * This module owns only the rail's look and its click target. `main.js` is
 * the single source of truth: it feeds `sync()` the turns it already renders
 * (and their statuses) and `setActive()` the turn it currently considers in
 * view. Nothing here reads the transcript DOM or touches scrolling itself —
 * it hands an id back to `onSelect` and lets the caller move the page.
 *
 * The dot is a real <button> (not a div) so it is keyboard-reachable, and its
 * accessible name carries the question number and outcome — colour is never
 * the only signal (DESIGN-INVARIANTS §8 / WCAG 1.4.1). The full question text
 * rides a floating `.conversation-tip` element on the navigator (deliberately
 * NOT a dot ::after: the rail is a scroll container and an overflow ancestor
 * clips anything that extends past it) — it opens to the right of whichever
 * dot is hovered, so the rail stays a quiet column of marks.
 */

"use strict";

import { t } from "./i18n.js";
import { fmt } from "./num.js";

const $ = (id) => document.getElementById(id);

/** One i18n key per `dashboard.js::turnStatus` value. Resolved through `t`
 * at dot-label build time, so a language switch (which rebuilds every turn
 * card in main.js) rebuilds these labels too. */
const STATUS_KEY = {
  ok: "navTurnOk",
  warn: "navTurnEmpty",
  bad: "navTurnFailed",
  running: "navTurnRunning",
  unknown: "navTurnUnknown",
};

/**
 * @param {{ onSelect: (turnId: string) => void }} opts
 */
export function createConversationNavigator(opts) {
  const nav = $("conversation-navigator");
  const list = nav ? nav.querySelector(".conversation-timeline") : null;
  if (!list) return null;

  const onSelect = opts && opts.onSelect;
  let activeId = null;

  // The floating tooltip. A child of the navigator and a sibling of the
  // timeline — so it is NOT inside the scroll container and cannot be clipped
  // when it opens to the right of a dot. Hidden until a dot is hovered/focused.
  const tip = document.createElement("div");
  tip.className = "conversation-tip";
  tip.setAttribute("role", "presentation");
  nav.appendChild(tip);

  function showTip(dot) {
    // getBoundingClientRect + style are real-browser-only: the test shim never
    // fires hover/focus, so this body stays out of its reach.
    const navRect = nav.getBoundingClientRect();
    const dotRect = dot.getBoundingClientRect();
    tip.textContent = dot.dataset.tip || "";
    // Open to the dot's LEFT (CSS `transform: translateX(-100%)` flips the
    // box to end at the left position set here, leaving a 10px gap on the
    // dot's side); vertically centred on it. Measured against the navigator
    // (the sticky containing block the tip is positioned within).
    tip.style.left = (dotRect.left - navRect.left - 10) + "px";
    tip.style.top = (dotRect.top - navRect.top + dotRect.height / 2) + "px";
    // The workspace is on the tip's left, so the cap is what's actually
    // available from the dot's left edge to the viewport's left edge (24px
    // safe margin); CSS max-width (280px) is the comfortable max when there
    // is room. The floor (80px) is for ultra-narrow windows where the dot
    // is within reach of the edge.
    const avail = dotRect.left - 10 - 24;
    tip.style.maxWidth = Math.min(280, Math.max(avail, 80)) + "px";
    tip.classList.add("show");
  }
  function hideTip() { tip.classList.remove("show"); }

  // The rail is a scroll container; scrolling it moves dots out from under the
  // floating tip, so a tip left open would point at the wrong question. Hide it
  // on the first scroll. Attached to the (reused) list, not per-dot, so it
  // never accumulates across syncs.
  list.addEventListener("scroll", hideTip, { passive: true });

  function rebuild(turns) {
    hideTip();                       // a rebuild (new question set) drops any open tip
    list.replaceChildren();
    for (const turn of turns) {
      list.appendChild(buildDot(turn));
    }
    if (activeId) setActive(activeId);
  }

  function setActive(turnId) {
    const newActive = turnId || null;
    const changed = newActive !== activeId;
    activeId = newActive;
    const target = activeId
      ? list.querySelector(`.conversation-dot[data-turn-id="${activeId}"]`)
      : null;
    list.querySelectorAll(".conversation-dot").forEach((btn) => {
      const current = btn === target;
      if (current) btn.setAttribute("aria-current", "true");
      else btn.removeAttribute("aria-current");
      btn.classList.toggle("is-active", current);
    });
    // Keep the active dot inside the rail's visible band. Without this, once
    // the chat grows past ~15 turns the analyst scrolls the transcript down
    // to question #50 and the highlighted dot is still off-screen at the top
    // of the (now scrollable) rail. Only run when the active dot actually
    // changed — updateNavigatorActive() in main.js fires on every rAF during
    // a page scroll, so a hard-scroll on every frame would fight the reader's
    // own manual rail scrolling.
    //
    // Capability guard: getBoundingClientRect + scrollTop + clientHeight don't
    // exist on the test shim's makeEl result, so this body stays out of its
    // reach (setActive IS called by the harness; the real DOM always has them).
    if (changed && target && typeof list.getBoundingClientRect === "function"
        && "clientHeight" in list && "scrollHeight" in list) {
      const listRect = list.getBoundingClientRect();
      const dotRect = target.getBoundingClientRect();
      // Dot's offset within the scrollable content = on-screen offset +
      // whatever the list is currently scrolled by.
      const dotInContent = (dotRect.top - listRect.top) + (list.scrollTop || 0);
      const ideal = dotInContent + dotRect.height / 2 - list.clientHeight / 2;
      list.scrollTop = Math.max(0, Math.min(ideal, list.scrollHeight - list.clientHeight));
    }
  }

  function buildDot(turn) {
    // Each dot lives in its own <li> so the timeline is a real list; the
    // connector is a CSS ::before on the li, not a separate element, so the
    // mark stays a single focusable control.
    const li = document.createElement("li");

    const button = document.createElement("button");
    button.type = "button";
    button.className = `conversation-dot is-${turn.status || "ok"}`;
    if (turn.collapsed) button.classList.add("is-collapsed");
    button.dataset.turnId = turn.turn_id;
    const label = dotLabel(turn);
    button.setAttribute("aria-label", label);
    // data-tip (not `title`) so the styled ::after tooltip is the only one —
    // a native title would double it. aria-label keeps the same text for AT.
    button.dataset.tip = label;
    button.addEventListener("click", () => {
      if (typeof onSelect === "function") onSelect(turn.turn_id);
    });
    // Hover/focus reveal the floating tip beside this dot (mouse + keyboard).
    button.addEventListener("pointerenter", () => showTip(button));
    button.addEventListener("pointerleave", hideTip);
    button.addEventListener("focus", () => showTip(button));
    button.addEventListener("blur", hideTip);

    li.appendChild(button);
    return li;
  }

  function dotLabel(turn) {
    const index = fmt(turn.index);
    const status = t(STATUS_KEY[turn.status]) || t("navTurnOk");
    const question = (turn.question || "").trim();
    return `${index} · ${status}${question ? " — " + question : ""}`;
  }

  return {
    sync: rebuild,
    setActive,
    /** @returns {string|null} */
    getActive: () => activeId,
  };
}
