/* web/js/sql-display.js — the one place that decides how generated SQL
 * looks on screen.
 *
 * Three layers, in order, all presentation-only:
 *
 *   1. Source of truth for COPY: Turn.sql_display || Turn.sql, verbatim.
 *      Never read the DOM. Never run the client formatter on this string.
 *
 *   2. Source of truth for DISPLAY: formatSqlForDisplay(...). Prefers an
 *      already-multi-line string (backend pretty_sql / hand-authored
 *      scenario SQL) and only prettifies one-liners via the vendored
 *      sql-formatter (T-SQL). That keeps two formatters from fighting:
 *      sqlglot on the server, sql-formatter on the client, same visual
 *      contract — clause-per-line — without re-flowing work the backend
 *      already did.
 *
 *   3. Highlighting: vendored Prism + a small T-SQL patch (bracketed
 *      identifiers, N'…' national strings). Failure at any layer leaves
 *      the previous layer's text intact.
 *
 * Loaded as a plain ES module from turn.js. Prism and sql-formatter are
 * globals from classic <script> tags in index.html (no build step — see
 * web/README.md).
 */

"use strict";

/** Client prettify options — T-SQL, editor-default readability. */
export const SQL_FORMAT_OPTIONS = Object.freeze({
  language: "tsql",
  keywordCase: "upper",
  tabWidth: 2,
  linesBetweenQueries: 1,
});

/**
 * Whether *sqlText* is already laid out for reading (multi-line).
 *
 * Backend `pretty_sql` and the simulated scenario SQL in
 * `web/js/data.js` both arrive this way. Re-running a second formatter
 * over them only risks churn (different indent, different line breaks)
 * for zero readability gain.
 *
 * @param {string | null | undefined} sqlText
 * @returns {boolean}
 */
function isAlreadyFormatted(sqlText) {
  return typeof sqlText === "string" && sqlText.includes("\n");
}

/**
 * Prettify SQL for DISPLAY only. Never use the return value as the copy
 * source of truth — see module docstring.
 *
 * Multi-line input is returned unchanged. One-liners go through the
 * vendored `window.sqlFormatter` with :data:`SQL_FORMAT_OPTIONS`. Missing
 * or throwing formatter falls back to the input unchanged.
 *
 * @param {string | null | undefined} sqlText
 * @returns {string} display SQL
 *
 * @example
 * formatSqlForDisplay("SELECT a FROM t")
 * // → "SELECT\n  a\nFROM\n  t"  (shape depends on sql-formatter)
 * formatSqlForDisplay("SELECT\n  a\nFROM t")
 * // → unchanged
 */
export function formatSqlForDisplay(sqlText) {
  if (!sqlText) return sqlText;
  if (isAlreadyFormatted(sqlText)) return sqlText;
  try {
    if (window.sqlFormatter && typeof window.sqlFormatter.format === "function") {
      return window.sqlFormatter.format(sqlText, SQL_FORMAT_OPTIONS);
    }
  } catch {
    /* Cosmetic only — fall through. */
  }
  return sqlText;
}

/**
 * The exact string the copy button must hand the clipboard.
 *
 * @param {{ sql_display?: string | null, sql?: string | null }} turn
 * @returns {string | null | undefined}
 */
export function copySourceOfTruth(turn) {
  return turn.sql_display || turn.sql;
}

/**
 * Display string for a Turn: source of truth, then client prettify rules.
 *
 * @param {{ sql_display?: string | null, sql?: string | null }} turn
 * @returns {string | null | undefined}
 */
export function displaySqlForTurn(turn) {
  return formatSqlForDisplay(copySourceOfTruth(turn));
}

/**
 * Patch Prism's generic SQL grammar for the T-SQL shapes this product
 * actually emits (see `security/sql_guard.pretty_sql` and the engine's
 * generated statements).
 *
 * Fixes two real mis-highlights found by running the vendored Prism
 * against a T-SQL sample:
 *
 * * `[Order]` was tokenised as the keyword `ORDER` inside the brackets —
 *   a table name painted as a reserved word. Bracketed identifiers are
 *   now one `identifier` token.
 * * `N'cement'` left the `N` outside the string token. The national
 *   string prefix is now part of the string.
 *
 * Idempotent: sets a flag on the language object so a second call is a
 * no-op. Safe to call when Prism or the SQL component is missing.
 *
 * @param {object | null | undefined} Prism - the global Prism namespace
 * @returns {void}
 */
export function patchPrismForTsql(Prism) {
  if (!Prism || !Prism.languages || !Prism.languages.sql) return;
  const sql = Prism.languages.sql;
  if (sql.__tsqlPatched) return;

  // Bracketed identifier: [Order], [Sales_Fact]. Must win over the bare
  // keyword rule that would otherwise claim the word inside the brackets.
  const bracketId = {
    pattern: /(^|[^@\[\]])(?:\[(?:[^\]\r\n]|]]|\\[\s\S])*\])/,
    lookbehind: true,
    greedy: true,
    inside: {
      punctuation: /^\[|\]$/,
    },
  };

  const existingIds = sql.identifier
    ? Array.isArray(sql.identifier)
      ? sql.identifier
      : [sql.identifier]
    : [];
  sql.identifier = [bracketId, ...existingIds];

  // National string: N'…'. Keep the generic string rules after it.
  const nationalString = {
    pattern: /(^|[^@\w])N'(?:[^']|'')*'/i,
    lookbehind: true,
    greedy: true,
  };
  const existingStrings = sql.string
    ? Array.isArray(sql.string)
      ? sql.string
      : [sql.string]
    : [];
  sql.string = [nationalString, ...existingStrings];

  sql.__tsqlPatched = true;
}

/**
 * Render *displaySql* into *codeEl* with Prism, presentation-only.
 *
 * Always assigns plain `textContent` first; highlighting only overlays
 * markup. Any Prism failure leaves the plain text in place.
 *
 * @param {HTMLElement} codeEl
 * @param {string} displaySql
 * @param {object | null | undefined} [Prism] - defaults to `window.Prism`
 * @returns {void}
 */
export function highlightSql(codeEl, displaySql, Prism) {
  const lib = Prism !== undefined ? Prism : (typeof window !== "undefined" ? window.Prism : undefined);
  codeEl.textContent = displaySql;
  try {
    patchPrismForTsql(lib);
    if (lib && lib.languages && lib.languages.sql) {
      codeEl.innerHTML = lib.highlight(displaySql, lib.languages.sql, "sql");
    }
  } catch {
    /* Presentation only — plain text already set. */
  }
}
