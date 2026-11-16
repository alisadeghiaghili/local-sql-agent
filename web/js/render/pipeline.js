/* web/js/render/pipeline.js — the five-step pipeline list, per turn.
 * Driven locally by timers in SIMULATED mode, or by SSE `stage` events in
 * LIVE mode (see main.js askLive). Same five stages as the old demo. */

"use strict";

/* The interpretation stage is the product's premium step — the one that
 * costs the analyst rows of their own result — so it gets its own mark
 * (a brain) and the gold treatment instead of sitting in the same row of
 * plain stage pills as the mechanics. */
const BRAIN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/></svg>';

const STEPS = [
  { key: "understand", label: "درک — تعیین زمینه از روی نشست و پرسش" },
  { key: "generate", label: "تولید — ساخت SQL با مدل زبانی محلی" },
  { key: "validate", label: "اعتبارسنجی — لایهٔ نگهبانی امنیتی و سقف ردیف" },
  { key: "execute", label: "اجرا — روی پایگاه‌داده" },
  { key: "interpret", label: "تفسیر — خلاصهٔ ساده‌شدهٔ نتیجه", gold: true, icon: BRAIN_ICON },
];

/** The connector between two stages: an arrow, never a number — the
 * strip reads as a path the question travels, not as a checklist. */
function stageArrow(className) {
  const arrow = document.createElement("span");
  arrow.className = className;
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "→";
  return arrow;
}

/** Slim stage strip for the default turn chrome (DESIGN.md §5.2).
 *
 * Five labels joined by arrows, not five cards. `setStage` matches renderPipeline so
 * SSE `stage` events and simulated timers drive both the strip and the
 * full list in the details drawer.
 *
 * @returns {{ el: HTMLElement, setStage: (key: string, state: string | null) => void, steps: string[] }}
 */
export function renderStageStrip() {
  const root = document.createElement("div");
  root.className = "stage-strip";
  root.setAttribute("role", "status");
  root.setAttribute("aria-live", "polite");

  const items = {};
  STEPS.forEach((step, i) => {
    if (i > 0) root.appendChild(stageArrow("stage-strip-arrow"));
    const item = document.createElement("span");
    item.className = "stage-strip-item";
    if (step.gold) item.classList.add("is-gold");
    item.dataset.step = step.key;
    if (step.icon) {
      const mark = document.createElement("span");
      mark.className = "stage-strip-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.innerHTML = step.icon;
      item.appendChild(mark);
    }
    const short = document.createElement("span");
    short.className = "stage-strip-label";
    // Short labels for the strip; the full sentence lives in the drawer.
    short.textContent = step.label.split("—")[0].trim();
    item.appendChild(short);
    root.appendChild(item);
    items[step.key] = item;
  });

  const summary = document.createElement("span");
  summary.className = "stage-strip-summary";
  root.appendChild(summary);

  function setStage(key, stateName) {
    const item = items[key];
    if (!item) return;
    item.classList.remove("running", "done", "error");
    if (stateName) item.classList.add(stateName);
    const done = Object.values(items).filter((n) => n.classList.contains("done")).length;
    const err = Object.values(items).some((n) => n.classList.contains("error"));
    if (err) summary.textContent = "ناتمام — خطا";
    else if (done >= STEPS.length) summary.textContent = `${done}/${STEPS.length}`;
    else if (done > 0) summary.textContent = `${done}/${STEPS.length}`;
    else summary.textContent = "";
  }

  return { el: root, setStage, steps: STEPS.map((s) => s.key) };
}
export function renderPipeline() {
  const list = document.createElement("div");
  list.className = "steps";
  list.setAttribute("role", "list");
  STEPS.forEach((step, i) => {
    if (i > 0) list.appendChild(stageArrow("step-arrow"));
    const li = document.createElement("span");
    li.className = step.gold ? "step is-gold" : "step";
    li.dataset.step = step.key;
    li.setAttribute("role", "listitem");
    if (step.icon) {
      const icon = document.createElement("span");
      icon.className = "step-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = step.icon;
      li.appendChild(icon);
    }
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = step.label;
    const status = document.createElement("span");
    status.className = "step-status";
    li.appendChild(label);
    li.appendChild(status);
    list.appendChild(li);
  });

  function setStage(key, stateName) {
    const li = list.querySelector(`.step[data-step="${key}"]`);
    if (!li) return;
    li.classList.remove("running", "done", "error");
    if (stateName) li.classList.add(stateName);
  }

  return { el: list, setStage, steps: STEPS.map((s) => s.key) };
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

/** Animates all five stages to "done" with realistic timing, for SIMULATED
 * mode. Pass a shorter plan (fewer/zero ms) to skip animation entirely.
 * `onStageSettled(key, state)` fires after each stage reaches "done" or
 * "error", so callers can progressively reveal card sections in step with
 * the pipeline instead of dumping the whole turn on screen at once. */
export async function runSimulatedStages(setStage, { failAt = null, plan, onStageSettled } = {}) {
  const timings = plan || [
    ["understand", 260], ["generate", 520], ["validate", 260], ["execute", 340], ["interpret", 300],
  ];
  for (const [key, ms] of timings) {
    setStage(key, "running");
    await delay(ms);
    if (failAt === key) {
      setStage(key, "error");
      if (onStageSettled) onStageSettled(key, "error");
      return;
    }
    setStage(key, "done");
    if (onStageSettled) onStageSettled(key, "done");
  }
}
