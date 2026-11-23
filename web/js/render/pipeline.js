/* web/js/render/pipeline.js — the five-step pipeline list, per turn.
 * Driven locally by timers in SIMULATED mode, or by SSE `stage` events in
 * LIVE mode (see main.js askLive). Same five stages as the old demo. */

"use strict";

/* Stage marks. Each stage gets a small geometric icon on its 24×24 stroke
 * grid (1.6px, currentColor — matching the rest of the chrome, see
 * web/js/icons.js) so the strip reads as a path, not a numbered checklist.
 *
 * The interpretation stage is the product's premium step — the one that
 * costs the analyst rows of their own result — so it keeps its own mark
 * (a magnifying glass with a "?" in the lens — "asking the data") plus the
 * gold treatment (see style.css). */
const ICON_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
const UNDERSTAND_ICON = `<svg ${ICON_ATTRS}><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>`;
const GENERATE_ICON = `<svg ${ICON_ATTRS}><path d="M8 9l-4 3 4 3"/><path d="M16 9l4 3-4 3"/><path d="M13 6l-2 12"/></svg>`;
const VALIDATE_ICON = `<svg ${ICON_ATTRS}><path d="M12 3l8 3v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-3z"/><path d="M9 12l2 2 4-4"/></svg>`;
const EXECUTE_ICON = `<svg ${ICON_ATTRS}><path d="M8 5v14l11-7z"/></svg>`;
/* Magnifier (lens + handle) with a "?" IN the lens — the "asking the data"
 * mark. The glyph is lucide's question hook scaled to fit the r=7 lens:
 * an arc that curls over and back down, then the dot — a plain vertical
 * line + dot would read as "!" instead. */
const INTERPRET_ICON = `<svg ${ICON_ATTRS}><circle cx="11" cy="11" r="7"/><path d="M8.9 8.9a2.1 2.1 0 0 1 4.1 .7c0 1.4-2.1 2.1-2.1 2.1"/><path d="M11 14.5h.01"/><path d="M15.8 15.8 20 20"/></svg>`;

const STEPS = [
  { key: "understand", label: "درک — تعیین زمینه از روی نشست و پرسش", icon: UNDERSTAND_ICON },
  { key: "generate", label: "تولید — ساخت SQL با مدل زبانی محلی", icon: GENERATE_ICON },
  { key: "validate", label: "اعتبارسنجی — لایهٔ نگهبانی امنیتی و سقف ردیف", icon: VALIDATE_ICON },
  { key: "execute", label: "اجرا — روی پایگاه‌داده", icon: EXECUTE_ICON },
  { key: "interpret", label: "تفسیر — خلاصهٔ ساده‌شدهٔ نتیجه", gold: true, icon: INTERPRET_ICON },
];

/** Slim stage strip for the default turn chrome (DESIGN.md §5.2).
 *
 * Five icon-nodes on a connected track: circular SVG icons (eye, code,
 * shield, play, magnifier-with-?) with the short step name below each. A
 * thin connector line joins adjacent nodes and fills with the state colour
 * as each step settles. The interpret node (magnifier) is gold and glows
 * when reached. `setStage` matches renderPipeline so SSE `stage` events and
 * simulated timers drive both the strip and the full list in the drawer.
 *
 * @returns {{ el: HTMLElement, setStage: (key: string, state: string | null) => void, steps: string[] }}
 */
export function renderStageStrip() {
  const root = document.createElement("div");
  root.className = "stage-strip";
  root.setAttribute("role", "status");
  root.setAttribute("aria-live", "polite");

  const items = {};
  const last = STEPS.length - 1;
  STEPS.forEach((step, i) => {
    // A step is a vertical column (node + label). The connector to the next
    // step is a sibling, so the row reads: step, line, step, line, step…
    const item = document.createElement("span");
    item.className = "stage-step";
    if (step.gold) item.classList.add("is-gold");
    item.dataset.step = step.key;
    // The node itself is a stage icon; the short label below it names the step.
    // `aria-label` carries the full sentence for screen readers.
    item.setAttribute("aria-label", step.label);

    const node = document.createElement("span");
    node.className = "stage-node";
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = step.icon || "";
    item.appendChild(node);

    const label = document.createElement("span");
    label.className = "stage-step-label";
    label.textContent = step.label.split("—")[0].trim();
    item.appendChild(label);

    root.appendChild(item);
    items[step.key] = item;

    // Connector line to the next node (a sibling of the steps, not a child,
    // so it sits between them on the node axis); the final step has none.
    if (i < last) {
      const line = document.createElement("span");
      line.className = "stage-line";
      line.setAttribute("aria-hidden", "true");
      root.appendChild(line);
    }
  });

  const summary = document.createElement("span");
  summary.className = "stage-strip-summary";
  root.appendChild(summary);

  function setStage(key, stateName) {
    const item = items[key];
    if (!item) return;
    item.classList.remove("running", "done", "error");
    if (stateName) item.classList.add(stateName);
    if (stateName === "running") item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    const done = Object.values(items).filter((n) => n.classList.contains("done")).length;
    const err = Object.values(items).some((n) => n.classList.contains("error"));
    if (err) summary.textContent = "ناتمام — خطا";
    else if (done > 0) summary.textContent = `${done}/${STEPS.length}`;
    else summary.textContent = "";
  }

  return { el: root, setStage, steps: STEPS.map((s) => s.key) };
}
export function renderPipeline() {
  // Class includes "steps" because run_turn_anatomy.mjs:160 asserts on it.
  // Layout: horizontal stepper (same as .stage-strip) but with the full
  // stage description under each node, since the drawer has room for it.
  const list = document.createElement("div");
  list.className = "steps pipeline";
  list.setAttribute("role", "list");

  const last = STEPS.length - 1;
  STEPS.forEach((step, i) => {
    const item = document.createElement("div");
    item.className = "pipeline-step" + (step.gold ? " is-gold" : "");
    item.dataset.step = step.key;
    item.setAttribute("role", "listitem");
    item.setAttribute("aria-label", step.label);

    const node = document.createElement("div");
    node.className = "pipeline-node";
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = step.icon || "";
    item.appendChild(node);

    // Full label + description, shown below the node.
    const text = document.createElement("div");
    text.className = "pipeline-text";
    const parts = step.label.split("—").map((p) => p.trim());
    const label = document.createElement("span");
    label.className = "pipeline-label";
    label.textContent = parts[0] || step.label;
    text.appendChild(label);
    const desc = parts.slice(1).join("—").trim();
    if (desc) {
      const d = document.createElement("span");
      d.className = "pipeline-desc";
      d.textContent = desc;
      text.appendChild(d);
    }
    item.appendChild(text);

    list.appendChild(item);

    if (i < last) {
      const line = document.createElement("span");
      line.className = "pipeline-line";
      line.setAttribute("aria-hidden", "true");
      list.appendChild(line);
    }
  });

  function setStage(key, stateName) {
    const item = list.querySelector(`.pipeline-step[data-step="${key}"]`);
    if (!item) return;
    item.classList.remove("running", "done", "error");
    if (stateName) item.classList.add(stateName);
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
