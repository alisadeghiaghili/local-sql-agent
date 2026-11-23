# NLQ Recall / Precision Benchmark

A 100-question, requirement-level benchmark that measures how faithfully the
NLQ system **interprets** Persian business questions over the IME auction
warehouse — i.e. which filters, entities, metrics, dates, groupings, orderings
and exclusions it extracts — rather than whether the final SQL string matches a
reference.

This complements (does not replace) the existing `eval/` gate:
- `eval/recall.py` — table-*selection* recall (retrieval only).
- `eval/cli.py run` — result-*fingerprint* regression gate (does the executed
  answer match a reference).

This directory scores the **semantic interpretation** in between.

## Files

| File | What it is |
|---|---|
| `recall_precision_test_cases.json` | The 100 questions + hand-validated ground truth. **Read-only input.** |
| `generate_test_cases.py` | Regenerates the JSON from its `CASES` list (the editable source of truth). |
| `evaluate_recall_precision.py` | Runs the two scoring layers and writes the results. |
| `make_report.py` | Renders `recall_precision_report.md` from the latest run JSON. |
| `recall_precision_results.csv` | One row per case (Layer A + B detail). |
| `recall_precision_run.json` | Full per-item detail + aggregates (report input). |
| `recall_precision_report.md` | The comprehensive human-readable report. |
| `.layer_b_cache.json` | Cache of generated SQL so re-scoring does not re-call the model. |

## Categories (100 total)

| Category | n |
|---|---:|
| Simple and direct | 20 |
| Multi-condition | 20 |
| Analytical / comparative | 20 |
| Ambiguous / indirect | 15 |
| Date & time range | 10 |
| Aggregation / statistical | 10 |
| Negative / exclusion | 5 |

## Two scoring layers

- **Layer B — the headline.** Full production pipeline: real model
  (`Qwen3.8-27B-FP8`) + live `Auction_DM` warehouse. The generated T-SQL is
  parsed back (sqlglot) into a requirement set and scored. Needs the LLM
  endpoint and DB.
- **Layer A — a diagnostic.** Deterministic pre-LLM retrieval only
  (`ValueRetriever` ring/date facets). No model, no warehouse. Scores only the
  ring + date facets; metric-column and entity choice are the model's job and
  belong to Layer B.

Scoring is semantic, not string equality:
- ring names match by **stored value** (e.g. `تالار سیمان`);
- relative dates and exclusions match by **column**;
- ordering matches by **sort direction** (the model orders by an aggregate alias);
- grouping matches by the **grouped column**;
- `COUNT(col.ID)` is treated as equivalent to `COUNT(*)`.

Per case: **Recall** = required items extracted / required items;
**Precision** = correctly extracted / all extracted. Missing items never count
against precision; unsupported extras never count against recall. Macro = mean
of per-case values; Micro = pooled across cases.

## How to run

From the repository root. Set `PYTHONIOENCODING=utf-8` on Windows so Persian
output does not hit the cp1252 console codec.

```bash
# 1) (re)generate the 100 cases from the editable CASES list
py eval/recall_precision/generate_test_cases.py

# 2) Layer A only — no model, no warehouse (fast CI signal)
py eval/recall_precision/evaluate_recall_precision.py

# 3) Both layers — live model + warehouse (the headline run; ~100 LLM calls,
#    cached to .layer_b_cache.json so a re-run only re-scores)
py eval/recall_precision/evaluate_recall_precision.py --live

# useful flags
py eval/recall_precision/evaluate_recall_precision.py --live --limit 8      # smoke
py eval/recall_precision/evaluate_recall_precision.py --live --ids RP-001,RP-042
py eval/recall_precision/evaluate_recall_precision.py --live --no-cache     # force re-call

# 4) render the markdown report from the latest run
py eval/recall_precision/make_report.py
```

Live mode requires the LLM endpoint and `Auction_DM_PASSWORD`/`Future_DM_PASSWORD`
to be present in `.env` (same as the production app); it reuses
`eval.pipeline.make_production_pipeline`, so it exercises the exact same
guard/correction/row-cap path the server does.

## Reproducibility notes

- The benchmark is **deterministic given a fixed model**: `LLM_TEMPERATURE` and
  `LLM_SEED` are already set in the deployment config. The generated SQL for
  each question is cached (`.layer_b_cache.json`), so re-scoring after a
  harness change does not re-call the model.
- Ground truth is independent of the model output and was validated against the
  live schema, `project_config` (entities/aliases/business-rules), and the
  `sima-db-report` reporting procedures. Filter values (ring names, customer
  types, currencies, contract kinds, date bounds 1380–1408) are real warehouse
  values, not invented.
- To re-baseline after a pipeline change: `--no-cache`, re-run `--live`, then
  `make_report.py`.

## Current headline (see report for full breakdown)

Layer B over 100 live cases: **Recall ≈ 85.6% (macro) / 87.5% (micro)**,
**Precision ≈ 83.4% (macro) / 85.2% (micro)**. The dominant error is
**cross-warehouse mis-routing** — 14/100 generic trade/price/volume questions
resolve to the futures `Trade` fact instead of the auction facts.
