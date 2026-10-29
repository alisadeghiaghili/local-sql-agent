/* web/admin/time.js — readable dates in the admin panel.
 *
 * The server sends ISO-8601 strings ("2026-10-27T10:46:31.435668"); the
 * panel printed them as they came. formatWhen() turns one into a Persian
 * calendar date and a time ("۵ آبان ۱۴۰۵، ۱۰:۴۶"), and keeps the original
 * string for the element's title, so the exact value is a hover away.
 *
 * The audit log and most admin records are written with the server's
 * local clock and no offset; a string without an offset is read as the
 * browser's local time, which is the same clock when the panel is opened
 * where the server runs. Anything that does not parse as a date (an empty
 * value, a placeholder) is returned unchanged, so nothing is ever lost.
 *
 * Pure (no DOM), so it is testable under Node.
 */

"use strict";

const ISO_LIKE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Parse an ISO-8601 string, or return null. More than three fractional
 * digits (Python's microseconds) are cut to milliseconds first: not every
 * browser parses six.
 *
 * @param {unknown} value
 * @returns {Date|null}
 */
export function parseIso(value) {
  if (typeof value !== "string") return null;
  const s = value.trim();
  if (!ISO_LIKE.test(s)) return null;
  const d = new Date(s.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * A readable Persian date and time for *value*, and the original string.
 *
 * @param {unknown} value — an ISO-8601 string from the server.
 * @param {{ locale?: string, timeZone?: string }} [opts] — for tests.
 * @returns {{ text: string, title: string }} `text` is the readable form,
 *   or the value unchanged when it is not a date; `title` is the original
 *   string ("" when there was none).
 *
 * @example
 *   formatWhen("2026-10-27T10:46:31.435668").text  // "۵ آبان ۱۴۰۵، ۱۰:۴۶"
 *   formatWhen("—").text                            // "—"
 */
export function formatWhen(value, opts = {}) {
  const original = value === null || value === undefined ? "" : String(value);
  const d = parseIso(original);
  if (!d) return { text: original, title: "" };
  const locale = opts.locale || "fa-IR";
  const hasTime = /[T ]\d{2}:\d{2}/.test(original);
  // A bare date is parsed as midnight UTC; shown in UTC it stays the same
  // calendar day wherever the browser is.
  const zone = opts.timeZone || (hasTime ? null : "UTC");
  const tz = zone ? { timeZone: zone } : {};
  const datePart = d.toLocaleDateString(locale, { year: "numeric", month: "long", day: "numeric", ...tz });
  if (!hasTime) return { text: datePart, title: original };
  const timePart = d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false, ...tz });
  return { text: `${datePart}، ${timePart}`, title: original };
}
