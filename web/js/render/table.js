/* web/js/render/table.js — renders a Turn's `result` block: the column/row
 * table (dir="ltr" inside the RTL page), row-count + truncated indicator,
 * and the `warnings` list, which contract §2 requires to be impossible to
 * miss (rendered above the table, not tucked below it). */

"use strict";

const nf = new Intl.NumberFormat("en-US");

function fmtCell(value, type) {
  if (value === null || value === undefined) return "—";
  if (type === "number" || typeof value === "number") {
    return Number.isFinite(value) ? nf.format(value) : String(value);
  }
  return String(value);
}

/** @param {import("../api.js").Result|null} result */
export function renderResult(result) {
  const wrap = document.createElement("div");
  wrap.className = "result-block";

  if (!result) {
    const p = document.createElement("p");
    p.className = "empty-result";
    p.textContent = "نتیجه‌ای در دسترس نیست (اجرا کامل نشد).";
    wrap.appendChild(p);
    return wrap;
  }

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
  wrap.appendChild(meta);

  if (!result.columns || result.columns.length === 0 || result.rows.length === 0) {
    const p = document.createElement("p");
    p.className = "empty-result";
    p.textContent = "بدون ردیف نتیجه.";
    wrap.appendChild(p);
    return wrap;
  }

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
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  wrap.appendChild(tableWrap);

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
