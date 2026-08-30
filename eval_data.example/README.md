# eval_data.example

This directory contains a **template golden set** for `eval_data/`. It
shows the required format for `golden.jsonl` — with sanitised, made-up
questions and answers standing in for the real customer data the team's
actual evaluation set is built from.

## Setup

1. Copy this directory to `eval_data/` (which is git-ignored):
   ```bash
   cp -r eval_data.example/ eval_data/
   ```
2. Replace `eval_data/golden.jsonl` with real, hand-verified questions and
   reference SQL from actual usage (see "Adding a real case" below).
3. Never commit `eval_data/` to git — it contains real customer questions
   and, in `expected_rows`, real data values.

## Running against this template set

```bash
# Offline / CI mode — no database, no LLM, replays the recorded answers:
.venv/Scripts/python.exe -m eval.cli run --golden eval_data.example/golden.jsonl

# Live mode — real Ollama + real database, for recording baselines and
# measuring latency:
.venv/Scripts/python.exe -m eval.cli run --golden eval_data.example/golden.jsonl --live
```

## File format

`golden.jsonl` is one JSON object per line, each one an
[`eval.models.GoldenCase`](../eval/models.py). Fields:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Stable, unique identifier. Used in reports and as the join key against a baseline. |
| `question` | yes | The natural-language question, verbatim as a user would type it. Persian, English, or mixed. |
| `tags` | no | Free-form keywords for the per-tag accuracy breakdown, e.g. `["customer", "count"]`. |
| `expected_sql` | yes, unless `expect` is `"out_of_scope"` | The reference T-SQL query a domain expert verified produces the correct answer. |
| `expect` | no (default `"success"`) | `"success"`, `"empty"` (reference answer is zero rows), `"out_of_scope"` (the generator should refuse), or `"error"` (reserved for the harness's own tests). |
| `expected_fingerprint` | required for offline mode | The order-insensitive result hash from `eval.fingerprint.fingerprint_dataframe`, precomputed from `expected_rows`. |
| `expected_rows` | required for offline mode (unless `out_of_scope`) | The reference query's result rows, as a list of `{column: value}` dicts. Used only by the offline replay executor — never sent to a real database. |
| `notes` | no | Free text explaining *why* this case exists / what it guards against. |

### One example row

```json
{
  "id": "customer_count_basic",
  "question": "How many customers are there?",
  "tags": ["customer", "count", "english"],
  "expected_sql": "SELECT COUNT(*) AS CustomerCount FROM [Auction_Dim].[Customer]",
  "expected_rows": [{"CustomerCount": 12840}],
  "expected_fingerprint": "233648abb0dda38e478e9df65e616551e0343c49adcd203f27ea12f0fe7eeb1e",
  "expect": "success",
  "notes": "Simplest possible aggregate — sanity baseline for the harness itself."
}
```

## What this template set covers

The 14 cases in `golden.jsonl` are a deliberately varied sample, not a
minimal one — each dimension the real set needs to cover appears at least
once:

* Persian questions with Persian (Eastern Arabic) digits, e.g. `۱۴۰۲`
  (`order_count_persian_year_1402`, `contracts_tir_1403_fa`) — the digits
  must normalise to ASCII before hitting a numeric column.
* Persian questions containing ZWNJ / نیم‌فاصله (`active_suppliers_zwnj`)
  — the half-space must survive tokenisation unmangled.
* Plain English and plain Persian questions with no special characters.
* Aggregations: `COUNT`, `SUM`, `AVG`.
* Jalali (Persian calendar) date filtering, including a year-and-month
  combination (`contracts_tir_1403_fa`), and the same kind of filter
  phrased as an English question (`contracts_year_1401_en`).
* Joins across a fact table and one or more dimension tables, including a
  `GROUP BY` / `TOP N` ranking query (`top5_symbols_by_value`,
  `top_broker_by_purchase_value_fa`, `ring_highest_volume_fa`).
* A deliberately out-of-scope question (`weather_out_of_scope`) — the
  schema has no table that could possibly answer it, so the generator
  must raise `OUT_OF_SCOPE` rather than hallucinate SQL.
* A question whose correct answer is a genuinely empty result set
  (`customers_with_placeholder_nationalcode_empty`, `expect: "empty"`) —
  distinct from an aggregate query, which always returns exactly one row
  even when the count is zero.

## Adding a real case

1. Pick a real question a user asked (or a representative variant —
   strip anything identifying).
2. Get a domain expert to write (or verify) the correct T-SQL for it.
3. Run that SQL against the real database and record the result rows.
4. Compute the fingerprint the harness will compare against:
   ```python
   import pandas as pd
   from eval.fingerprint import fingerprint_dataframe

   rows = [...]  # the rows you just recorded
   print(fingerprint_dataframe(pd.DataFrame(rows)))
   ```
5. Append one JSON line to `eval_data/golden.jsonl` with `expected_sql`,
   `expected_rows`, and the `expected_fingerprint` from step 4.
6. Run `python -m eval.cli run --golden eval_data/golden.jsonl` and
   confirm the new case passes (this also re-validates every reference
   query against the live `security.sql_guard` rules).

For a case you want to *record* rather than hand-verify (e.g. seeding a
baseline from the model's current best answer), run in `--live` mode
without an `expected_fingerprint`, inspect the reported
`actual_fingerprint` for that case in the JSON output, and copy it back
into the golden file once you've confirmed the returned rows are correct
— the harness does not do this substitution automatically, since an
unreviewed "recorded" answer being treated as ground truth would defeat
the point of the golden set.
