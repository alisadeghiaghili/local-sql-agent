/* web/js/num.js — one place that decides how a number looks.
 *
 * There used to be seven. `chart.js` declared BOTH an `en-US` formatter and
 * an `fa-IR` one and used them side by side in the same component — the
 * "مجموع" tile in Latin digits next to the "تعداد نقاط" tile in Persian.
 * `llm-status.js` did the same thing inside one stat grid; `table.js`,
 * `turn.js`, `sessions.js` and `memory.js` each made the call again
 * locally. That is not one bug: it is the same decision taken seven times,
 * differently, and it is why the numbers looked unsettled.
 *
 * Two rules, and the second is the one that is easy to get wrong.
 *
 * ONE DIGIT SYSTEM FOR EVERYTHING A READER READS. The interface is
 * Persian, so counts, measures, percentages and durations are Persian
 * digits. Consistency is the whole point; a mix reads as an accident
 * because it is one.
 *
 * LATIN DIGITS SURVIVE ONLY WHERE THE DIGITS ARE NOT PROSE. Token counts
 * an operator pastes into a ticket, identifiers, anything inside a
 * `dir="ltr"` block, and SQL itself stay Latin — those are values that
 * travel to other systems, and converting them makes them wrong somewhere
 * else. `technical()` is that carve-out, and it is deliberate rather than
 * left over.
 *
 * Bidi: a number rendered inside a Persian sentence is isolated, because
 * an un-isolated numeral run next to Persian text lets the separators and
 * any adjacent punctuation reorder. `inText()` does that; the plain
 * formatters are for standalone cells and SVG, where the surrounding
 * direction is already fixed. */

"use strict";

const FA = new Intl.NumberFormat("fa-IR");
const LATIN = new Intl.NumberFormat("en-US");

/** The default: a number a person reads, in the interface's own digits. */
export function fmt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? FA.format(n) : "—";
}

/** A value that travels to another system — tokens, ids, LTR content.
 *
 * Kept Latin on purpose. Converting a figure someone will paste into a
 * ticket or compare against a server log makes it wrong at the other end,
 * which is worse than a mixed-looking page. */
export function technical(value) {
  const n = Number(value);
  return Number.isFinite(n) ? LATIN.format(n) : "—";
}

/** A percentage, already in 0..100. */
export function pct(value, digits = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${FA.format(Number(n.toFixed(digits)))}٪`;
}

/** A rate given as either 0..1 or 0..100, rendered as a percentage.
 *
 * The backend reports some rates one way and some the other; guessing
 * once here beats every call site guessing separately. */
export function rate(value, digits = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return pct(n <= 1 ? n * 100 : n, digits);
}

/** Milliseconds, as a person would say them. */
export function ms(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n < 1000) return `${fmt(Math.round(n))} میلی‌ثانیه`;
  return `${fmt(Number((n / 1000).toFixed(1)))} ثانیه`;
}

/** Wrap a formatted number for safe placement inside Persian prose.
 *
 * U+2066 LEFT-TO-RIGHT ISOLATE … U+2069 POP DIRECTIONAL ISOLATE. Without
 * it, a numeral run adjacent to Persian text can drag neighbouring
 * punctuation to the wrong side — the subtle half of "the numbers look
 * wrong" that survives fixing the digit system.
 */
export function inText(formatted) {
  return `⁦${formatted}⁩`;
}
