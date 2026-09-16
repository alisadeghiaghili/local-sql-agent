/* web/js/prism-tsql-patch.js
 *
 * Classic script (not an ES module). Load AFTER prism.min.js and
 * prism-sql.min.js via <script defer>, BEFORE any Prism.highlight.
 * Must share Prism's script realm — do not import this from an ES module
 * and expect the grammar to stay healthy.
 *
 * Fixes two T-SQL mis-highlights in the stock Prism SQL grammar:
 *   [Order] was tokenised as the keyword ORDER inside the brackets
 *   N'…' left the N outside the string token
 *
 * Idempotent via __tsqlPatched on the language object.
 * Exposes window.patchPrismForTsql for re-entry from sql-display.js.
 */
(function (window) {
  "use strict";

  /**
   * @param {object | null | undefined} lib Prism namespace
   * @returns {void}
   */
  function patchPrismForTsql(lib) {
    if (!lib || !lib.languages || !lib.languages.sql) return;
    if (lib.languages.sql.__tsqlPatched) return;

    var sql = Object.assign({}, lib.languages.sql);

    sql.identifier = [{
      pattern: /\[[^\]]+\]/,
      greedy: true,
    }].concat(Array.isArray(sql.identifier) ? sql.identifier : [sql.identifier]);

    sql.string = [{
      pattern: /N'[^']*'/,
      greedy: true,
    }].concat(Array.isArray(sql.string) ? sql.string : [sql.string]);

    // Non-enumerable on purpose: Prism tokenises by iterating the grammar's
    // enumerable keys and using each value as a token. A plain
    // `sql.__tsqlPatched = true` puts a boolean in that iteration, Prism calls
    // `.exec` on it, throws, and highlightSql falls back to plain text -- so
    // this "already patched" marker silently killed all SQL highlighting. It
    // stays readable (the guard above reads it) but hidden from enumeration.
    Object.defineProperty(sql, "__tsqlPatched", {
      value: true,
      enumerable: false,
      configurable: true,
      writable: true,
    });
    lib.languages.sql = sql;
  }

  window.patchPrismForTsql = patchPrismForTsql;
  patchPrismForTsql(window.Prism);
})(window);
