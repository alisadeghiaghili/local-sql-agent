/* web/js/export.js — client-side CSV export for a rendered result.
 *
 * `exporters/excel_exporter.py` exists in the backend, but it is wired only
 * to the CLI/wizard flow (`app.py`) — no HTTP route exposes it. Only
 * /query, /query/stream, /cache/*, /v2/sessions* and /health exist (see
 * api/server.py, api/v2_routes.py). Calling a fictitious endpoint here
 * would be exactly the kind of fabricated request api.js's docstring says
 * this UI must never make, so instead this module does the one thing that
 * is honestly achievable client-side: serialize the rows already rendered
 * in the browser to CSV and hand the browser a real download. It reaches
 * only what is already on screen, nothing the server hasn't already sent.
 */

"use strict";

const UTF8_BOM = "﻿";

function csvEscape(value) {
  const s = value === null || value === undefined ? "" : String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

/** @param {import("./api.js").Result} result */
export function resultToCsv(result) {
  const lines = [];
  lines.push(result.columns.map((c) => csvEscape(c.name)).join(","));
  for (const row of result.rows) {
    lines.push(result.columns.map((c) => csvEscape(row[c.name])).join(","));
  }
  return lines.join("\r\n");
}

/** Triggers a real browser download of *result* as CSV. A UTF-8 BOM is
 * prefixed so Excel — the tool this affordance exists for — opens Persian
 * text correctly instead of mangling it into mojibake. */
export function downloadResultAsCsv(result, filenameBase) {
  const csv = UTF8_BOM + resultToCsv(result);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${filenameBase || "result"}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
