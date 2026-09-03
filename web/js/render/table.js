/* web/js/render/table.js — renders a Turn's `result` block: shape
 * selection plus the concrete presentations for each shape, and the
 * `warnings` list, which contract §2 requires to be impossible to miss
 * (rendered above the table, not tucked below it).
 *
 * Shape selection (`determineShape`) is driven entirely by the contract
 * fields `session/models.py::TurnResult` actually carries — `columns[].type`
 * (one of "number" | "string" | "boolean" | "datetime", see
 * `session/engine.py::_infer_type`), `rows.length`, and `row_count` — never
 * by sniffing cell values. See the brief table this implements:
 *
 *   1 row  x 1 numeric column        -> scalar   (one big figure)
 *   1 row  x several columns         -> record   (record card)
 *   <=30 rows, 1 label + 1 measure   -> chart     (chart.js) + table, switch
 *   many numeric columns / many rows -> table
 *   non-numeric only                 -> table
 *   0 rows                           -> empty     (see renderEmptyResult)
 */

"use strict";

import { renderChartAndTable } from "./chart.js";
import { downloadResultAsCsv } from "../export.js";
import { SOURCE_LABELS } from "./assumptions.js";

const nf = new Intl.NumberFormat("en-US");

const CHART_MAX_ROWS = 30;

export const SHAPE = {
  EMPTY: "empty",
  SCALAR: "scalar",
  RECORD: "record",
  CHART: "chart",
  TABLE: "table",
};

/** Priority order for guessing which assumption is most likely responsible
 * for a zero-row result — see renderEmptyResult's docstring below. Lower
 * number = checked first. `question`-sourced assumptions are the user's
 * own words, so they are the last thing to suspect. */
const CULPRIT_PRIORITY = { session: 0, default: 1, policy: 2, question: 3 };

export function fmtCell(value, type) {
  if (value === null || value === undefined) return "—";
  if (type === "number" || typeof value === "number") {
    return Number.isFinite(value) ? nf.format(value) : String(value);
  }
  return String(value);
}

/** Pure, contract-driven shape selection — no value sniffing. `result` must
 * be a non-null TurnResult ({columns, rows, row_count, truncated}); the
 * "no result object at all" case (LLM/transport failure) is handled by the
 * caller (renderResult) before this is ever invoked. */
export function determineShape(result) {
  const rows = result.rows || [];
  const columns = result.columns || [];
  const rowCount = typeof result.row_count === "number" ? result.row_count : rows.length;

  if (rowCount === 0 || rows.length === 0) return SHAPE.EMPTY;
  if (columns.length === 0) return SHAPE.TABLE;

  const numericCols = columns.filter((c) => c.type === "number");

  if (rows.length === 1 && columns.length === 1 && numericCols.length === 1) {
    return SHAPE.SCALAR;
  }
  if (rows.length === 1 && columns.length > 1) {
    return SHAPE.RECORD;
  }
  if (columns.length === 2 && numericCols.length === 1 && rows.length >= 2 && rows.length <= CHART_MAX_ROWS) {
    return SHAPE.CHART;
  }
  return SHAPE.TABLE;
}

/** Picks the assumption most likely responsible for a zero-row result —
 * see renderEmptyResult. Pure and total: returns null only when there are
 * no assumptions to point at all. */
export function pickLikelyWrongAssumption(assumptions) {
  if (!assumptions || assumptions.length === 0) return null;
  const nonQuestion = assumptions.filter((a) => a.source !== "question");
  const pool = nonQuestion.length ? nonQuestion : assumptions;
  return pool.slice().sort(
    (a, b) => (CULPRIT_PRIORITY[a.source] ?? 9) - (CULPRIT_PRIORITY[b.source] ?? 9),
  )[0];
}

/* ── Shape renderers ──────────────────────────────────────────────── */

function renderMeta(result) {
  const meta = document.createElement("div");
  meta.className = "result-meta";
  const rowLabel = document.createElement("span");
  rowLabel.textContent = `${result.row_count.toLocaleString("fa-IR")} ردیف`;
  meta.appendChild(rowLabel);
  if (result.truncated) {
    const flag = document.createElement("span");
    flag.className = "truncated-flag";
    flag.textContent = "کوتاه‌شده — نمایش بخشی از نتیجه";
    meta.appendChild(flag);
  }
  return meta;
}

/** 0 rows: the query ran and the guard allowed it, so this is very rarely
 * "the warehouse has nothing" — it is almost always one inherited
 * assumption (most often session-scoped, per the brief) turning out
 * wrong for this question. Says so explicitly and points at the most
 * likely culprit among `assumptions`, instead of a bare "no rows found"
 * that invites the wrong conclusion. Styled in the STATUS colour (warn),
 * never the brand colour — this is a fact about the result, not chrome. */
export function renderEmptyResult(assumptions) {
  const wrap = document.createElement("div");
  wrap.className = "zero-rows";

  const head = document.createElement("div");
  head.className = "zero-rows-head";
  head.textContent = "صفر ردیف — احتمالاً یکی از مفروضه‌ها اشتباه است، نه اینکه داده‌ای وجود ندارد";
  wrap.appendChild(head);

  const body = document.createElement("div");
  body.className = "zero-rows-body";

  const lead = document.createElement("p");
  lead.textContent =
    "پرس‌وجو به‌درستی اجرا شد و لایهٔ نگهبانی امنیتی اجازه داد — پس مسئله دادهٔ خالی نیست. " +
    "به‌احتمال زیاد یکی از مفروضه‌های زیر (به‌ویژه مواردی که از نشست یا پیش‌فرض آمده‌اند) با این پرسش نمی‌خواند.";
  body.appendChild(lead);

  const culprit = pickLikelyWrongAssumption(assumptions);
  if (culprit) {
    const hint = document.createElement("p");
    hint.className = "zero-rows-culprit";
    const strong = document.createElement("b");
    strong.textContent = `${culprit.field}: ${culprit.value}`;
    hint.appendChild(document.createTextNode("مشکوک‌ترین مورد: "));
    hint.appendChild(strong);
    const tag = document.createElement("span");
    tag.className = "zero-rows-tag";
    tag.dataset.source = culprit.source;
    tag.textContent = SOURCE_LABELS[culprit.source] || culprit.source;
    hint.appendChild(tag);
    body.appendChild(hint);
  }

  if (assumptions && assumptions.length) {
    const list = document.createElement("ul");
    list.className = "zero-rows-list";
    for (const a of assumptions) {
      const li = document.createElement("li");
      li.textContent = `${a.field}: ${a.value} (${SOURCE_LABELS[a.source] || a.source})`;
      list.appendChild(li);
    }
    body.appendChild(list);
  } else {
    const note = document.createElement("p");
    note.textContent = "برای این پرسش مفروضه‌ای ثبت نشده — اگر نتیجه نادرست به نظر می‌رسد، پرسش را با جزئیات بیشتری دوباره امتحان کنید.";
    body.appendChild(note);
  }

  wrap.appendChild(body);
  return wrap;
}

/** 1 row x 1 numeric column: one large figure. No chart, no table — a
 * table would waste the frame on a single cell, and a chart has nothing
 * to plot a shape from. */
function renderScalar(result) {
  const col = result.columns[0];
  const row = result.rows[0];

  const wrap = document.createElement("div");
  wrap.className = "scalar-block";

  const fig = document.createElement("span");
  fig.className = "scalar-figure num";
  fig.textContent = fmtCell(row[col.name], col.type);
  wrap.appendChild(fig);

  const note = document.createElement("span");
  note.className = "scalar-note";
  note.textContent = `${col.name} · یک ردیف، یک ستون — نموداری در کار نیست`;
  wrap.appendChild(note);

  return wrap;
}

/** 1 row x several columns: a record card (field/value pairs), not a
 * one-row table — a table with a single body row wastes a header row on
 * headers nobody scans as a group. */
function renderRecord(result) {
  const row = result.rows[0];
  const wrap = document.createElement("dl");
  wrap.className = "record-grid";

  for (const col of result.columns) {
    const item = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = col.name;
    const dd = document.createElement("dd");
    dd.textContent = fmtCell(row[col.name], col.type);
    if (col.type === "number") dd.classList.add("num");
    item.appendChild(dt);
    item.appendChild(dd);
    wrap.appendChild(item);
  }
  return wrap;
}

/** The plain column/row table — used standalone for the `table` shape and
 * embedded (behind the view switch) for the `chart` shape. Kept ltr as a
 * whole block, matching the existing convention for tables of identifiers
 * and numbers. */
export function renderTableOnly(result) {
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "result-table";
  table.dir = "ltr";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const col of result.columns) {
    const th = document.createElement("th");
    th.textContent = col.name;
    th.scope = "col";
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const row of result.rows) {
    const tr = document.createElement("tr");
    for (const col of result.columns) {
      const td = document.createElement("td");
      td.textContent = fmtCell(row[col.name], col.type);
      if (col.type === "number") td.classList.add("n");
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  return tableWrap;
}

/** The export affordance (contract point 7): `exporters/excel_exporter.py`
 * exists in the backend but no HTTP route exposes it (only /query,
 * /query/stream, /cache/*, /v2/sessions* — see api/server.py,
 * api/v2_routes.py). Rather than invent an endpoint this UI has no
 * business assuming exists, this downloads a real CSV built from the rows
 * already rendered in the browser — see ../export.js. */
export function renderExportRow(result) {
  const bar = document.createElement("div");
  bar.className = "result-actions";
  const info = document.createElement("span");
  info.textContent = `${result.columns.length.toLocaleString("fa-IR")} ستون`;
  bar.appendChild(info);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-export";
  btn.textContent = "دانلود CSV";
  btn.addEventListener("click", () => downloadResultAsCsv(result));
  bar.appendChild(btn);
  return bar;
}

/**
 * @param {import("../api.js").Result|null} result
 * @param {{assumptions?: import("../api.js").Assumption[], guardRejected?: boolean}} [opts]
 */
export function renderResult(result, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "result-block";

  if (!result) {
    const p = document.createElement("p");
    p.className = "empty-result";
    p.textContent = "نتیجه‌ای در دسترس نیست (اجرا کامل نشد).";
    wrap.appendChild(p);
    return wrap;
  }

  const shape = determineShape(result);
  wrap.dataset.shape = shape;

  if (shape === SHAPE.EMPTY) {
    // The "probably a wrong assumption, not missing data" framing only
    // applies when the query actually ran (guard allowed it) and matched
    // nothing — see this module's docstring / the brief's §4. When the
    // guard REJECTED the statement, nothing executed at all, so that
    // framing would be false; the SQL section already shows the guard's
    // rejection reason, so this stays a plain, honest note instead.
    if (opts.guardRejected) {
      const p = document.createElement("p");
      p.className = "empty-result";
      p.textContent = "بدون ردیف — پرس‌وجو پیش از اجرا توسط لایهٔ نگهبانی امنیتی رد شد (جزئیات در بخش SQL بالا).";
      wrap.appendChild(p);
      return wrap;
    }
    wrap.appendChild(renderEmptyResult(opts.assumptions));
    return wrap;
  }

  if (shape === SHAPE.SCALAR) {
    // No generic "N rows" meta line here — renderScalar's own note already
    // says "one row, one column", so a meta row above it would just repeat
    // the same fact in different words instead of adding information.
    wrap.appendChild(renderScalar(result));
    return wrap;
  }

  wrap.appendChild(renderMeta(result));

  if (shape === SHAPE.RECORD) {
    wrap.appendChild(renderRecord(result));
    return wrap;
  }
  if (shape === SHAPE.CHART) {
    wrap.appendChild(renderChartAndTable(result));
    return wrap;
  }

  wrap.appendChild(renderTableOnly(result));
  wrap.appendChild(renderExportRow(result));
  return wrap;
}

/** @param {string[]} warnings */
export function renderWarnings(warnings) {
  if (!warnings || warnings.length === 0) return null;
  const wrap = document.createElement("div");
  wrap.className = "warnings";
  for (const w of warnings) {
    const item = document.createElement("div");
    item.className = "warning-item";
    item.setAttribute("role", "alert");
    const icon = document.createElement("span");
    icon.className = "warn-icon";
    icon.textContent = "⚠";
    icon.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.textContent = w;
    item.appendChild(icon);
    item.appendChild(text);
    wrap.appendChild(item);
  }
  return wrap;
}
