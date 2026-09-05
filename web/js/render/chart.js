/* web/js/render/chart.js — the `chart` shape (table.js's determineShape):
 * <=30 rows, one non-numeric label column plus one numeric measure column.
 *
 * Renders, top to bottom:
 *   1. a "story strip" — 2-3 framings of the SAME rows, named by the
 *      message they'd tell ("the year trended up until Aban") rather than
 *      by chart type, each with a one-line reason; an inappropriate
 *      framing (a pie over many categories) is shown explicitly rejected
 *      with its reason instead of silently omitted.
 *   2. the chosen framing's chart (line for a trend, horizontal bar for a
 *      ranking) — Storytelling with Data craft: the title states the
 *      takeaway, dimensions move to a small subcaption, no gridlines or
 *      frame, at most two reference lines, direct labels only on the
 *      point(s) that carry the message, everything else grey
 *      (--chart-context) with the point in the brand hue (--chart-focus).
 *   3. a small figures strip (three tiles) summarising the same rows.
 *   4. a view switch to the plain table (table.js's renderTableOnly) plus
 *      the export affordance.
 *
 * KNOWN LIMITATION — chart focus basis. The API does not say which part
 * of a series the LLM's interpretation sentence is actually about (no
 * span/offset into the series, nothing like it in
 * `docs/api-contract-v2.md` §4), so this module cannot literally
 * highlight "what the sentence means" the way a hand-authored caption
 * could. `chooseFocus` below derives a focus point from the data itself
 * (the maximum, for a trend or ranking reading; the latest point, for a
 * before/after reading) and always NAMES the rule it used — in the
 * subcaption text and the chart's `title` attribute — rather than
 * drawing a highlight the reader has no way to verify. The better
 * long-term fix is for the backend to say which rows the interpretation
 * emphasises; that is out of scope here.
 *
 * KNOWN LIMITATION — partial/incomplete periods. `TurnResult` carries no
 * period-boundary metadata (no way to know whether the last row is, say,
 * a calendar month still in progress), so this module does not attempt
 * to flag an incomplete final period the way the design prototype did
 * for a specific domain (Persian calendar months) — doing that here
 * generically would mean guessing at row semantics the contract does not
 * expose, which is exactly the kind of value-sniffing table.js's
 * `determineShape` deliberately avoids for shape selection. That honesty
 * marker would need the backend to say a period is incomplete.
 */

"use strict";

import { renderTableOnly, renderExportRow, fmtCell } from "./table.js";

// Formatting lives in one place now -- this module used to declare both
// an en-US formatter and an fa-IR one and use them in adjacent tiles.
import { fmt as faNum } from "../num.js";
const nf = { format: faNum };

const FOCUS_RULE_LABEL_FA = {
  max: "بیشترین مقدار",
  latest: "آخرین نقطه",
  largest_change: "بزرگ‌ترین تغییر بین دو نقطهٔ پیاپی",
  none: "—",
};

/* ── Pure logic — testable without a DOM ─────────────────────────────── */

function numericValues(rows, measureKey) {
  return rows.map((r) => {
    const v = Number(r[measureKey]);
    return Number.isFinite(v) ? v : 0;
  });
}

function lineHeadline(rows, labelKey, values, maxIdx) {
  const lastIdx = values.length - 1;
  if (maxIdx === lastIdx) return `روند تا ${rows[lastIdx][labelKey]} صعودی بود`;
  if (maxIdx === 0) return `مقدار از همان ${rows[0][labelKey]} رو به کاهش بود`;
  return `روند تا ${rows[maxIdx][labelKey]} صعودی بود، سپس آرام گرفت`;
}

function indexOfMax(values) {
  let idx = 0;
  for (let i = 1; i < values.length; i++) if (values[i] > values[idx]) idx = i;
  return idx;
}

/** Builds 2-4 named framings of the same (rows, labelKey, measureKey),
 * plus, once the category count makes it genuinely illegible, one
 * explicitly REJECTED framing (pie) with its reason — never silently
 * omitted, per the brief: an analyst looking for that option deserves an
 * answer. Pure: no DOM access. */
export function chooseFramings(rows, labelKey, measureKey) {
  const values = numericValues(rows, measureKey);
  const n = values.length;
  const maxIdx = indexOfMax(values);

  const framings = [
    {
      kind: "line",
      label: "خط",
      headline: lineHeadline(rows, labelKey, values, maxIdx),
      reason: "سنجه در طول یک توالی است؛ خط پیوستگی روند را نشان می‌دهد و با جملهٔ تفسیر می‌خواند.",
      rejected: false,
    },
    {
      kind: "bar",
      label: "میلهٔ افقی مرتب",
      headline: `${rows[maxIdx][labelKey]} بیشترین مقدار را داشت`,
      reason: "اگر رتبه مهم است نه توالی — میلهٔ افقی برای برچسب فارسی خواناتر است.",
      rejected: false,
    },
  ];

  if (n >= 4) {
    const half = Math.floor(n / 2);
    const firstHalf = values.slice(0, half).reduce((a, b) => a + b, 0);
    const secondHalf = values.slice(half).reduce((a, b) => a + b, 0);
    const pct = firstHalf === 0 ? null : Math.round(((secondHalf - firstHalf) / Math.abs(firstHalf)) * 100);
    framings.push({
      kind: "split-bar",
      label: "دو میله",
      headline:
        pct === null
          ? "مقایسهٔ نیمهٔ اول و دوم"
          : `نیمهٔ دوم ${faNum(Math.abs(pct))}٪ ${pct >= 0 ? "بیشتر" : "کمتر"} از نیمهٔ اول بود`,
      reason: "ساده‌ترین شکل اگر پیام یک مقایسهٔ دوتایی است؛ چند نقطه به دو عدد خلاصه می‌شود.",
      rejected: false,
    });
  }

  if (n > 6) {
    framings.push({
      kind: "pie",
      label: "دایره‌ای",
      headline: "سهم هر مورد از کل",
      reason: `مناسب نیست: مقایسهٔ ${faNum(n)} زاویه برای چشم دشوار است و ترتیب داده‌ها از بین می‌رود.`,
      rejected: true,
    });
  }

  return framings;
}

/** Derives which data point carries the message for a chosen framing, and
 * names the rule used — see this module's docstring. Pure: no DOM access. */
export function chooseFocus(values, framingKind) {
  if (!values.length) return { index: -1, rule: "none" };
  if (framingKind === "line" || framingKind === "bar") {
    return { index: indexOfMax(values), rule: "max" };
  }
  if (framingKind === "split-bar") {
    return { index: values.length - 1, rule: "latest" };
  }
  let idx = 0;
  let biggest = -1;
  for (let i = 1; i < values.length; i++) {
    const d = Math.abs(values[i] - values[i - 1]);
    if (d > biggest) { biggest = d; idx = i; }
  }
  return { index: idx, rule: "largest_change" };
}

/* ── DOM rendering ─────────────────────────────────────────────────── */

function figureTile(label, value) {
  const tile = document.createElement("div");
  tile.className = "figure-tile";
  const l = document.createElement("span");
  l.className = "figure-tile-label";
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "figure-tile-value num";
  v.textContent = value;
  tile.appendChild(l);
  tile.appendChild(v);
  return tile;
}

function renderFiguresStrip(rows, labelKey, values) {
  const wrap = document.createElement("div");
  wrap.className = "figures-strip";

  const total = values.reduce((a, b) => a + b, 0);
  wrap.appendChild(figureTile("مجموع", nf.format(total)));
  wrap.appendChild(figureTile("تعداد نقاط", faNum(values.length)));

  if (values.length >= 4) {
    const half = Math.floor(values.length / 2);
    const firstHalf = values.slice(0, half).reduce((a, b) => a + b, 0);
    const secondHalf = values.slice(half).reduce((a, b) => a + b, 0);
    const pct = firstHalf === 0 ? null : Math.round(((secondHalf - firstHalf) / Math.abs(firstHalf)) * 100);
    wrap.appendChild(
      figureTile("نیمهٔ دوم/اول", pct === null ? "—" : `${pct >= 0 ? "+" : ""}${faNum(pct)}٪`),
    );
  } else {
    const maxIdx = indexOfMax(values);
    wrap.appendChild(figureTile("بیشترین", `${nf.format(values[maxIdx])} · ${rows[maxIdx][labelKey]}`));
  }
  return wrap;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

/** Line chart. RTL time flow: row 0 (earliest) plots at the RIGHT edge,
 * the last row at the LEFT edge, matching the page's reading direction —
 * the same convention the design prototype used. Two reference lines at
 * most (baseline and the max value); everything before the focus segment
 * is context grey, the focus segment onward is the brand hue, with a
 * direct label only on the focus point. */

/* ── Keeping text inside the drawing ─────────────────────────────────────
 * SVG does not clip or reflow text: a label positioned near an edge simply
 * renders outside the viewBox and disappears. Both failure modes were
 * live here — a value label ran off the right of the bar chart when the
 * number was long, and the line chart's end-of-axis labels, anchored
 * `middle` at the extreme x positions, had half their width outside the
 * box on both sides.
 *
 * There is no layout to measure against while building a detached SVG, so
 * width is estimated from character count. The estimate only has to be
 * good enough to decide *which side to anchor on* and *when to clamp*,
 * not to place text precisely — an anchor decision that is one character
 * out still keeps the label inside. */

/** Rough advance width of `text` at the chart's label size, in user units. */
function estimateTextWidth(text, size = 11) {
  // The ratio is deliberately ABOVE the real advance width of both Persian
  // glyphs and Latin digits at these sizes. An estimate that merely
  // approximates the truth puts labels exactly on the boundary, where any
  // stricter measurement -- the geometry test's, or a real browser with a
  // different face loaded -- finds them outside. Over-estimating costs a
  // few pixels of gutter and makes the guarantee hold instead of usually
  // holding.
  return String(text).length * size * 0.68;
}

/** Anchor and x for a label that must stay within `[0, W]`.
 *
 * Near an edge the anchor flips so the text grows inward instead of over
 * the boundary, which is what `middle` at an extreme position cannot do. */
function edgeSafeLabel(x, text, W, size = 11) {
  const half = estimateTextWidth(text, size) / 2;
  if (x - half < 2) return { x: 2, anchor: "start" };
  if (x + half > W - 2) return { x: W - 2, anchor: "end" };
  return { x, anchor: "middle" };
}


/** Shorten `text` until its estimated width fits `maxWidth`, with slack.
 *
 * The slack matters: the width estimate is an approximation, and the one
 * the tests measure with is deliberately more generous than this one so
 * they err toward catching overflow. A renderer that truncated to exactly
 * its own estimate would sit right on the boundary and fail under any
 * stricter measurement -- including a real browser with a different face
 * loaded. Leaving room is what makes the guarantee hold rather than
 * merely usually hold. */
function truncateToWidth(text, maxWidth, size = 11) {
  const budget = maxWidth * 0.88;
  if (estimateTextWidth(text, size) <= budget) return text;
  let out = String(text);
  while (out.length > 1 && estimateTextWidth(`${out}…`, size) > budget) {
    out = out.slice(0, -1);
  }
  return `${out}…`;
}

function renderLineChart(rows, labelKey, values, focus) {
  const W = 640, H = 190;
  const padL = 40, padR = 12, padT = 22, padB = 34;
  const n = values.length;
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;

  // Both ends of this expression must use the same two paddings, or the
  // reserved space lands on one side while the plot starts on the other.
  // It previously began at padR and spanned (W - padL - padR), which left
  // the leading edge with 12px of room where 40 was intended.
  const xAt = (i) => padL + ((n - 1 - i) / Math.max(1, n - 1)) * (W - padL - padR);
  const yAt = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": `نمودار خطی؛ ${faNum(n)} نقطه، تمرکز روی ${rows[focus.index][labelKey]} (${FOCUS_RULE_LABEL_FA[focus.rule]}).`,
  });

  svg.appendChild(svgEl("line", { class: "chart-refline", x1: padL, y1: yAt(max), x2: W - padR, y2: yAt(max) }));
  svg.appendChild(svgEl("line", { class: "chart-refline", x1: padL, y1: yAt(0), x2: W - padR, y2: yAt(0) }));

  const segStart = Math.max(0, focus.index - 1);
  const contextPts = [];
  for (let i = 0; i <= segStart; i++) contextPts.push(`${xAt(i)},${yAt(values[i])}`);
  const focusPts = [];
  for (let i = segStart; i < n; i++) focusPts.push(`${xAt(i)},${yAt(values[i])}`);

  if (contextPts.length > 1) {
    svg.appendChild(svgEl("polyline", {
      fill: "none", stroke: "var(--chart-context)", "stroke-width": "2.5", "stroke-linejoin": "round",
      points: contextPts.join(" "),
    }));
  }
  svg.appendChild(svgEl("polyline", {
    fill: "none", stroke: "var(--chart-focus)", "stroke-width": "3", "stroke-linejoin": "round",
    points: focusPts.join(" "),
  }));

  const fx = xAt(focus.index), fy = yAt(values[focus.index]);
  svg.appendChild(svgEl("circle", { cx: fx, cy: fy, r: 4.5, fill: "var(--chart-focus)" }));
  const focusText = `${rows[focus.index][labelKey]} · ${fmtCell(values[focus.index], "number")}`;
  const focusPos = edgeSafeLabel(fx, focusText, W, 12);
  // Above the point normally; below it when the point sits so high that the
  // label's ascent would leave the top of the box. The focus point IS the
  // maximum often enough for this to matter.
  const above = fy - 10 >= 12;
  const label = svgEl("text", {
    class: "chart-focus-label", x: focusPos.x, y: above ? fy - 10 : fy + 18,
    "text-anchor": focusPos.anchor,
  });
  label.textContent = focusText;
  svg.appendChild(label);

  const axisIdx = n > 2 ? [0, Math.floor((n - 1) / 2), n - 1] : [0, n - 1];
  for (const i of axisIdx) {
    const text = String(rows[i][labelKey]);
    const pos = edgeSafeLabel(xAt(i), text, W);
    const t = svgEl("text", {
      class: "chart-axis-label", x: pos.x, y: H - 8, "text-anchor": pos.anchor,
    });
    t.textContent = text;
    svg.appendChild(t);
  }

  const maxLbl = svgEl("text", { class: "chart-value-label dim", x: padL + 2, y: Math.max(11, yAt(max) - 4), "text-anchor": "start" });
  maxLbl.textContent = fmtCell(max, "number");
  svg.appendChild(maxLbl);

  return svg;
}

/** Horizontal bar (ranking) chart. Rows sorted descending by value; bars
 * grow from the trailing edge outward, one direct value label per bar,
 * only the top-ranked bar in the focus colour. */
function renderBarChart(rows, labelKey, values, focus) {
  const W = 640;
  const rowH = 26, gap = 8;
  const n = values.length;
  const H = n * (rowH + gap) + gap;
  const labelW = 130;
  const max = Math.max(...values, 1);

  // Reserve the trailing gutter from the widest value that will actually
  // be drawn, not from a fixed guess. The constant used to be 60px, which
  // is fine for "1,240" and far too little for a rial figure in the
  // billions -- the label simply rendered past the edge of the viewBox and
  // vanished. Capped so a single enormous number cannot squeeze the bars
  // themselves down to nothing; past the cap the label moves inside the
  // bar instead (see below).
  const widestValue = Math.max(
    ...values.map((v) => estimateTextWidth(fmtCell(v, "number"))),
  );
  const padR = Math.min(Math.max(60, widestValue + 14), Math.round(W * 0.3));

  const order = values
    .map((v, i) => i)
    .sort((a, b) => values[b] - values[a]);

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": `نمودار میلهٔ افقی مرتب‌شده؛ ${rows[focus.index][labelKey]} در رتبهٔ اول (${FOCUS_RULE_LABEL_FA[focus.rule]}).`,
  });

  order.forEach((origIdx, rank) => {
    const y = gap + rank * (rowH + gap);
    const barMax = W - labelW - padR;
    const w = Math.max(2, (values[origIdx] / max) * barMax);
    const isFocus = origIdx === focus.index;

    // The category track is a fixed 130px; a longer name would run off the
    // leading edge, so it is truncated with an ellipsis and the full value
    // stays available to a screen reader through <title>.
    const rawName = String(rows[origIdx][labelKey]);
    const shown = truncateToWidth(rawName, labelW - 14);
    const label = svgEl("text", {
      class: "chart-axis-label", x: labelW - 8, y: y + rowH / 2 + 4, "text-anchor": "end",
    });
    label.textContent = shown;
    if (shown !== rawName) {
      const title = svgEl("title", {});
      title.textContent = rawName;
      label.appendChild(title);
    }
    svg.appendChild(label);

    svg.appendChild(svgEl("rect", {
      x: labelW, y, width: w, height: rowH, rx: 3,
      fill: isFocus ? "var(--chart-focus)" : "var(--chart-context)",
    }));

    // Outside the bar when the gutter can hold it; inside, right-aligned
    // against the bar's end, when it cannot. Placing it inside is the
    // standard answer for a long value on a long bar, and it is what keeps
    // the number visible instead of clipped at the boundary.
    const valueText = fmtCell(values[origIdx], "number");
    const valueW = estimateTextWidth(valueText);
    const fitsOutside = labelW + w + 8 + valueW <= W - 2;
    const valueLabel = svgEl("text", {
      class: `chart-value-label${isFocus ? "" : " dim"}${fitsOutside ? "" : " inside"}`,
      x: fitsOutside ? labelW + w + 8 : labelW + w - 8,
      y: y + rowH / 2 + 4,
      "text-anchor": fitsOutside ? "start" : "end",
    });
    valueLabel.textContent = valueText;
    svg.appendChild(valueLabel);
  });

  return svg;
}

/** The "split-bar" framing's own chart: exactly two bars, the rows'
 * values summed into a first-half bucket and a second-half bucket — NOT
 * the same per-row ranking chart `renderBarChart` draws. The second
 * bucket is always the focus bar, matching chooseFocus's "latest" rule
 * for this framing kind (the point of a before/after reading is the
 * later bucket, whichever direction it moved). */
function renderSplitBarChart(values) {
  const W = 640, rowH = 40, gap = 16;
  const H = 2 * rowH + 3 * gap;
  const labelW = 90;
  const half = Math.floor(values.length / 2);
  const firstHalf = values.slice(0, half).reduce((a, b) => a + b, 0);
  const secondHalf = values.slice(half).reduce((a, b) => a + b, 0);
  const buckets = [
    { label: "نیمهٔ اول", value: firstHalf, focus: false },
    { label: "نیمهٔ دوم", value: secondHalf, focus: true },
  ];
  const max = Math.max(firstHalf, secondHalf, 1);
  // Same fixed-gutter bug as the ranked bar chart: 70px cannot hold a
  // summed total, and these two bars are sums, so they are the largest
  // numbers this component ever draws.
  const padR = Math.min(
    Math.max(70, Math.max(
      estimateTextWidth(fmtCell(firstHalf, "number")),
      estimateTextWidth(fmtCell(secondHalf, "number")),
    ) + 14),
    Math.round(W * 0.3),
  );

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`,
    role: "img",
    "aria-label": `مقایسهٔ نیمهٔ اول (${fmtCell(firstHalf, "number")}) و نیمهٔ دوم (${fmtCell(secondHalf, "number")}).`,
  });

  buckets.forEach((b, i) => {
    const y = gap + i * (rowH + gap);
    const barMax = W - labelW - padR;
    const w = Math.max(2, (b.value / max) * barMax);
    const label = svgEl("text", { class: "chart-axis-label", x: labelW - 8, y: y + rowH / 2 + 4, "text-anchor": "end" });
    label.textContent = b.label;
    svg.appendChild(label);
    svg.appendChild(svgEl("rect", {
      x: labelW, y, width: w, height: rowH, rx: 4,
      fill: b.focus ? "var(--chart-focus)" : "var(--chart-context)",
    }));
    const valueText = fmtCell(b.value, "number");
    const fitsOutside = labelW + w + 8 + estimateTextWidth(valueText) <= W - 2;
    const valueLabel = svgEl("text", {
      class: `chart-value-label${b.focus ? "" : " dim"}${fitsOutside ? "" : " inside"}`,
      x: fitsOutside ? labelW + w + 8 : labelW + w - 8,
      y: y + rowH / 2 + 4,
      "text-anchor": fitsOutside ? "start" : "end",
    });
    valueLabel.textContent = valueText;
    svg.appendChild(valueLabel);
  });

  return svg;
}

/** @param {import("../api.js").Result} result */
export function renderChartAndTable(result) {
  const columns = result.columns;
  const rows = result.rows;
  const measureCol = columns.find((c) => c.type === "number");
  const labelCol = columns.find((c) => c !== measureCol);
  const measureKey = measureCol.name;
  const labelKey = labelCol.name;
  const values = numericValues(rows, measureKey);

  const framings = chooseFramings(rows, labelKey, measureKey);
  let activeIdx = 0;

  const wrap = document.createElement("div");
  wrap.className = "chart-block-wrap";

  const storyStrip = document.createElement("div");
  storyStrip.className = "story-strip";
  const storyHead = document.createElement("div");
  storyHead.className = "story-head";
  const storyHeadB = document.createElement("b");
  storyHeadB.textContent = "این نتیجه را چطور بگوییم؟";
  const storyHeadSpan = document.createElement("span");
  storyHeadSpan.textContent = `یک برچسب + یک سنجه · ${faNum(rows.length)} ردیف`;
  storyHead.appendChild(storyHeadB);
  storyHead.appendChild(storyHeadSpan);
  storyStrip.appendChild(storyHead);

  const opts = document.createElement("div");
  opts.className = "story-opts";
  framings.forEach((f, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "story-opt" + (f.rejected ? " rejected" : "");
    btn.setAttribute("aria-pressed", String(i === activeIdx));
    if (f.rejected) btn.disabled = true;
    const em = document.createElement("em");
    em.textContent = f.label;
    const b = document.createElement("b");
    b.textContent = f.headline;
    const span = document.createElement("span");
    span.textContent = f.reason;
    btn.appendChild(em);
    btn.appendChild(b);
    btn.appendChild(span);
    if (!f.rejected) {
      btn.addEventListener("click", () => {
        activeIdx = i;
        opts.querySelectorAll(".story-opt").forEach((el, j) => el.setAttribute("aria-pressed", String(j === activeIdx)));
        renderChart();
      });
    }
    opts.appendChild(btn);
  });
  storyStrip.appendChild(opts);
  wrap.appendChild(storyStrip);

  const chartBlock = document.createElement("div");
  chartBlock.className = "chart-block";
  const takeaway = document.createElement("p");
  takeaway.className = "chart-takeaway";
  const subcap = document.createElement("p");
  subcap.className = "chart-subcap";
  const chartHost = document.createElement("div");
  chartBlock.appendChild(takeaway);
  chartBlock.appendChild(subcap);
  chartBlock.appendChild(chartHost);
  wrap.appendChild(chartBlock);

  function renderChart() {
    const framing = framings[activeIdx];
    const focus = chooseFocus(values, framing.kind);
    takeaway.textContent = framing.headline;
    const ruleLabel = FOCUS_RULE_LABEL_FA[focus.rule] || focus.rule;
    subcap.textContent = `${measureKey} · ${labelKey} · تمرکز بر پایهٔ ${ruleLabel}`;
    subcap.title = `basis: ${focus.rule}`;
    chartHost.innerHTML = "";
    let svg;
    if (framing.kind === "bar") svg = renderBarChart(rows, labelKey, values, focus);
    else if (framing.kind === "split-bar") svg = renderSplitBarChart(values);
    else svg = renderLineChart(rows, labelKey, values, focus);
    chartHost.appendChild(svg);
  }
  renderChart();

  wrap.appendChild(renderFiguresStrip(rows, labelKey, values));

  const viewSwitch = document.createElement("div");
  viewSwitch.className = "view-switch";
  const chartBtn = document.createElement("button");
  chartBtn.type = "button";
  chartBtn.className = "view-btn active";
  chartBtn.textContent = "نمودار";
  const tableBtn = document.createElement("button");
  tableBtn.type = "button";
  tableBtn.className = "view-btn";
  tableBtn.textContent = "جدول";
  viewSwitch.appendChild(chartBtn);
  viewSwitch.appendChild(tableBtn);
  wrap.appendChild(viewSwitch);

  const tableHost = document.createElement("div");
  tableHost.hidden = true;
  tableHost.appendChild(renderTableOnly(result));
  wrap.appendChild(tableHost);

  chartBtn.addEventListener("click", () => {
    storyStrip.hidden = false;
    chartBlock.hidden = false;
    tableHost.hidden = true;
    chartBtn.classList.add("active");
    tableBtn.classList.remove("active");
  });
  tableBtn.addEventListener("click", () => {
    storyStrip.hidden = true;
    chartBlock.hidden = true;
    tableHost.hidden = false;
    tableBtn.classList.add("active");
    chartBtn.classList.remove("active");
  });

  wrap.appendChild(renderExportRow(result));
  return wrap;
}
