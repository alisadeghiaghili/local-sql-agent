/* web/js/render/pipeline.js — the five-step pipeline list, per turn.
 * Driven locally by timers in SIMULATED mode, or by SSE `stage` events in
 * LIVE mode (see main.js askLive). Same five stages as the old demo. */

"use strict";

const STEPS = [
  { key: "understand", label: "درک — تعیین زمینه از روی نشست و پرسش" },
  { key: "generate", label: "تولید — ساخت SQL با مدل زبانی محلی" },
  { key: "validate", label: "اعتبارسنجی — لایهٔ نگهبانی امنیتی و سقف ردیف" },
  { key: "execute", label: "اجرا — روی پایگاه‌داده" },
  { key: "interpret", label: "تفسیر — خلاصهٔ ساده‌شدهٔ نتیجه" },
];

/** Slim stage strip for the default turn chrome (DESIGN.md §5.2).
 *
 * Five dots/labels, not five cards. `setStage` matches renderPipeline so
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
    const item = document.createElement("span");
    item.className = "stage-strip-item";
    item.dataset.step = step.key;
    const mark = document.createElement("span");
    mark.className = "stage-strip-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = String(i + 1);
    const short = document.createElement("span");
    short.className = "stage-strip-label";
    // Short labels for the strip; the full sentence lives in the drawer.
    short.textContent = step.label.split("—")[0].trim();
    item.appendChild(mark);
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
  const ol = document.createElement("ol");
  ol.className = "steps";
  for (const step of STEPS) {
    const li = document.createElement("li");
    li.className = "step";
    li.dataset.step = step.key;
    const icon = document.createElement("span");
    icon.className = "step-icon";
    icon.textContent = String(STEPS.indexOf(step) + 1);
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = step.label;
    const status = document.createElement("span");
    status.className = "step-status";
    li.appendChild(icon);
    li.appendChild(label);
    li.appendChild(status);
    ol.appendChild(li);
  }

  function setStage(key, stateName) {
    const li = ol.querySelector(`.step[data-step="${key}"]`);
    if (!li) return;
    li.classList.remove("running", "done", "error");
    if (stateName) li.classList.add(stateName);
  }

  return { el: ol, setStage, steps: STEPS.map((s) => s.key) };
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
