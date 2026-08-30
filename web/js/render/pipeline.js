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

/** Builds the <ol> and returns { el, setStage } where setStage(key, state)
 * updates one step's visual state ("running" | "done" | "error" | null). */
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
