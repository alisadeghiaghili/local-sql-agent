# Recommended `project_config/` updates (from the recall/precision benchmark)

**Date:** 2026-11-22 · **Baseline run:** 100 live cases, Layer B macro **Recall 85.6% / Precision 83.4%**, 14/100 cross-warehouse mis-routes.
**Status:** *recommendations only* — no config file has been changed. Every claim below was
re-verified against the deployed `project_config/*.yaml` and the routing/retrieval source on
2026-11-22 (line numbers are from the current files).

> **How to validate any change:** re-run from the repo root
> `PYTHONIOENCODING=utf-8 py eval/recall_precision/evaluate_recall_precision.py --live --no-cache`
> then `py eval/recall_precision/make_report.py`, and compare against
> `recall_precision_report.md`. The benchmark is the acceptance test for all of this.

---

## Why config, and how the failure actually works (read this first)

The dominant error — **14/100 generic questions mis-routing to the futures/CSD warehouse**
(`Future_Fact.Trade`, CSD `vTrades`) — is produced by the **source-routing** step, not by the
model picking a wrong table, and not by the few-shot examples. Verified mechanism:

1. `datasources.yaml` configures **two** sources (`Auction_DM`, `Future_DM`) with
   `default: Auction_DM` (line 178). Because more than one source is configured,
   `retrieval/source_selector.py::select_source` runs and routes **each question to ONE
   source** before the prompt is built. Signals, in order: **keywords → session →
   retrieval evidence → default**.
2. `prompt_engine/source_scope.py::examples_for_source` and
   `static_prefix.build_static_prefix` then render **only that source's** tables,
   relationships and few-shot examples. So a question routed to `Future_DM` is shown *no
   auction tables at all* — the model cannot even see `Auction_Fact.Contract`.
3. Therefore the futures examples in `examples.yaml` (all of them say "در بازار آتی") are the
   **visible symptom** of an upstream routing decision, not the cause. Removing them does not
   fix a mis-route; fixing *routing* does.

The two routing signals a config edit can actually move for a generic question:

- **Keyword signal (#1):** won by the source with more distinct matching phrases. Auction's
  list is the *shortest* (`[Auction, فیزیکی, بازار فیزیکی]`, line 183) while futures has six
  specific ones. A question containing any auction word not in that list gets no auction hit.
- **Retrieval-evidence signal (#3):** scored from the tables the *table-retrieval* layer
  selects. That layer is driven by `retrieval_hints.yaml` (`always_include` force-includes at
  max score; `fact_patterns` fast-match) plus TF-IDF over table descriptions/aliases. The
  deployed `retrieval_hints.yaml` is **still the shipped English template** — `fact_tables:
  ["Order"]` and every trigger phrase is English (`order`, `ring`, `channel`, `spring`…). So
  the three main auction facts (`Contract`, `CustomerContract`, `Offer`) have **zero Persian
  force-include triggers** and are **mis-classified as non-fact tables** (`configured_fact_tables()`
  returns `{Order}` only); they can only compete via raw TF-IDF, which for generic words like
  "حجم/معاملات/بازار" ends up favouring `Future_Fact.Trade`/`vTrades`. `always_include`
  **overrides TF-IDF**, so filling it with the auction facts + Persian triggers is the
  single highest-leverage fix.

The secondary, config-fixable errors (dropped filters, wrong metric column, lost symbol
grouping, weekday off-by-one) are independent of routing and are addressed in §4–§8.

---

## Priority map

| # | File | Finding it fixes | Priority |
|---:|---|---|:---:|
| 1 | `retrieval_hints.yaml` | 14 mis-routes (table retrieval → routing signal #3); auction facts have no Persian coverage and are mis-classified as non-facts | **Critical** |
| 2 | `system_prompt.md` | 14 mis-routes (no "default = auction" rule) **and** a contradictory generic `## Schema` block the model reads before the real one | **Critical** |
| 3 | `datasources.yaml` (`keywords`) | 14 mis-routes (routing signal #1); auction keyword list is the shortest | High |
| 4 | `schema.yaml` (`resolvable_columns` / `prefetchable_columns`) | dropped offerkind (3×), currency (2×), customer-type (2×), clearingkind (1×), contract-kind/status, symbol (4×) filters | High |
| 5 | `examples.yaml` | rebalance to auction; fix `LIMIT` (T-SQL-forbidden) examples; add missed patterns (symbol group-by, active count, kind/currency filters) | Medium |
| 6 | `metrics.yaml` | 3× wrong metric column; still the `YourFactTable` template | Medium |
| 7 | `aliases.yaml` | 1 weekday off-by-one (RP-015); weekday words don't force the `Date` table | Medium |
| 8 | `entities.yaml` | customer-type / offer-kind / active value resolution | Medium |
| 9 | `session_policy.yaml` | hygiene (Persian `default_label`, clarification text); ring option list is already complete | Low |
| — | `business_rules.yaml`, `relationships.yaml`, `schema.yaml` structure | already correct — do not change | — |

---

## 1. `retrieval_hints.yaml` — fill the template so the auction facts win retrieval (Critical)

**Current state (verified):** the file is the unmodified English template.
`fact_tables: ["Order"]` (line 26), `always_include` covers only `Date`/`Order`/`Ring`/`Broker`/
`Supplier` with **English** triggers (`date`, `year`, `spring`, `order`, `channel`…), and
`fact_patterns` is just `Order: [order, purchase order]`. The header (line 21) says *"This
template mirrors … the example tables … and uses English-only trigger phrases."*

**Why it matters:** `always_include` force-includes a table at maximum score, which
`retrieval/table_evidence.py` feeds into routing signal #3. With no auction triggers, generic
questions fall to TF-IDF, which tips to futures. Adding the triggers + the correct fact-table
list is the strongest single lever against the 14 mis-routes.

**Change (replace the body; keep the header comments).** Use **fully-qualified table names** —
this `schema.yaml` mixes bare and qualified keys (`OfferKind` is bare, `Auction_Dim.Customer`
is qualified), and bare `Contract` is *ambiguous* (resolves to both `Auction_Fact.Contract`
and `Future_Dim.Contract`). Qualified names resolve exactly and leave no room for the
narrowing heuristic:

```yaml
fact_tables:
  - "Auction_Fact.Contract"
  - "CustomerContract"
  - "Offer"
  - "Order"
  - "AuctionLog"
  - "TalarLog"

always_include:
  Date:
    - "تاریخ"
    - "سال"
    - "ماه"
    - "هفته"
    - "روز"
    - "فصل"
    - "بهار"
    - "تابستان"
    - "پاییز"
    - "زمستان"
    - "هفتگی"
    - "ماهانه"
    - "سالیانه"
    - "سالانه"
    - "دوره"
  Auction_Fact.Contract:
    - "قرارداد"
    - "قراردادهای"
    - "مطابقت سالن"
    - "تطبیق سالن"
    - "حجم بازار"
    - "ارزش بازار"
  CustomerContract:
    - "قراردادهای مشتری"
    - "خرید مشتری"
    - "مشتری"
    - "مشتریان"
  Offer:
    - "عرضه"
    - "عرضه‌ها"
    - "پیشنهاد"
    - "ارزش عرضه"
  Order:
    - "سفارش"
    - "سفارش‌ها"
  Ring:
    - "رینگ"
    - "حلقه"
    - "تالار"
  Broker:
    - "کارگزار"
    - "کارگزاران"
    - "نماینده"
  Supplier:
    - "تأمین‌کننده"
    - "عرضه‌کننده"
    - "تولیدکننده"
  Auction_Dim.Customer:
    - "مشتری"
    - "مشتریان"
    - "خریدار"
  Symbol:
    - "نماد"
    - "نمادهای"
    - "کالا"
    - "محصول"

fact_patterns:
  Auction_Fact.Contract: [قرارداد, تطبیق, مطابقت]
  CustomerContract: [قراردادهای مشتری, مشتری]
  Offer: [عرضه, پیشنهاد]
  Order: [سفارش]
```

Notes:
- Drop the template's generic English triggers (`channel`, `request`, `spring`, `trading
  hall`). `channel` is a false friend in this domain and the English season words are dead
  weight now that the Persian ones are present.
- The force-include of `Auction_Fact.Contract` on generic "حجم بازار / ارزش بازار" phrasing
  is the direct antidote to the TF-IDF pull toward `Future_Fact.Trade`.

**Expected effect:** routing signal #3 now favours the auction facts for generic trade/volume/
value questions; the 14 mis-routes should collapse toward single digits. Re-run the benchmark
to confirm — this is the change the report will show most clearly.

---

## 2. `system_prompt.md` — remove the contradictory generic schema and add a domain rule (Critical)

**Current state (verified):** the file is the unmodified template. Beyond the header comment
("describes the generic example schema only"), it contains a **full `## Schema` section**
(lines 44–61) describing `[sales].[Order]`, `[sales].[Customer]`, `[ref].[Ring]`, etc., and
**`## Out-of-scope` examples** (weather/election/sports, lines 69–73).

**Why it matters:** `prompt_engine/static_prefix.py:238` assembles the prompt as
`{system_prompt}` **first**, then a separate `DATABASE SCHEMA` section rendered from the real
`schema.yaml`. So the model currently reads *two contradictory schemas* — the generic
`[sales]`/`[ref]` one at the very top, then the real `Auction_Fact.*`/`Auction_Dim.*` one
below. That is pure noise, and the top-of-prompt generic schema has no "default market" rule
to anchor generic questions to the auction warehouse.

**Change.**

1. **Delete the template's `## Schema` and `## Out-of-scope` sections entirely** (lines 44–73)
   — the real schema is already injected by the `DATABASE SCHEMA` section, so keeping the fake
   one only confuses the model. Keep the generic `## Output rules` (TOP-vs-LIMIT, no
   `SELECT *`, single-data-source, bracket-quoting) — all correct and dialect-relevant.

2. **Add a deployment-specific domain section** (in Persian, since the users are Persian) with
   four rules:

   1. **Default market rule.**
      «اگر بازار صریحا نام نبرد (آتی، مشتقه، اختیار معامله، گواهی سپرده، CSD/نمات)، سؤال را
      بر **بازار فیزیکی/حراجی** (`Auction_Fact.*` / `Auction_Dim.*`) تعبیر کن. فقط با وجود
      واژه‌ی «آتی/مشتقه/اختیار/CSD» از `Future_Fact.*` / `CDCMarket_Fact.*` استفاده کن.»
      (Model-side belt-and-braces for the routing fix in §1/§3.)
   2. **Metric anchoring** (mirrors `business_rules.yaml`, already correct):
      «ارزش/مبلغ/ریال → `TotalPrice`؛ حجم/مقدار/تن → `HallMatchingQuantity` (قرارداد) /
      `Quantity` (قرارداد مشتری) / `OfferQuantity` (عرضه)؛ تعداد → `COUNT(*)`.»
   3. **Ring rule:** «رینگ همیشه با نام کامل ذخیره‌شده فیلتر شود (مثلاً `تالار سیمان`)،
      نه با شماره.»
   4. **Out-of-scope for the auction default:** open interest / P&L / margin call / option
      exercise are futures-only concepts; for a physical-market user answering from
      `Auction_DM`, return `OUT_OF_SCOPE` rather than silently switching warehouses.

   The setup wizard can draft this from the schema; by hand it is a ~30-minute edit.

**Expected effect:** removes the contradictory-schema noise and gives the model an explicit
tie-break. Combined with §1 and §3 this is what should take the **ambiguous** category
(currently the worst, ≈58% recall) up to within ~10 points of **simple** (≈92%).

---

## 3. `datasources.yaml` — strengthen the auction routing keywords (High)

**Current state (verified):**
```yaml
Auction_DM:  keywords: [Auction, فیزیکی, بازار فیزیکی]                       # line 183
Future_DM:   keywords: [Future, مشتقه, بازار مشتقه, گواهی سپرده, آتی, اختیار معامله]  # line 198
```

**Why it matters:** routing signal #1 (keyword match). The auction list is the *shortest* and
matches none of the common auction words, so a question containing an auction word
(قرارداد/عرضه/مشتری/حراج) still gets **zero** auction keyword hits and relies on the weaker
signals. The futures list is already appropriately *specific*, so leave it — the fix is to
make the auction list cover the generic auction vocabulary.

**Change:**
```yaml
Auction_DM:
  keywords: [Auction, حراج, بازار فیزیکی, فیزیکی, قرارداد, عرضه, سفارش,
             مشتری, کارگزار, تالار, رینگ, نماد, ارزش معامله, تطبیق سالن]
```

Keep `Future_DM` **unchanged** — its specificity is what keeps generic questions from
keyword-routing to futures. (The `keywords` list is validated at start-up for duplicates /
folded-equivalents, so avoid two entries that fold to the same text.)

**Expected effect:** any question with an auction word now keyword-routes to `Auction_DM`
independently of retrieval evidence — a robustness gain on top of §1 (a *truly*
keyword-less question already defaults to `Auction_DM`).

---

## 4. `schema.yaml` — add `resolvable_columns` / `prefetchable_columns` to the small enums (High)

**Current state (verified):** the deployed `schema.yaml` has **no** `resolvable_columns` /
`prefetchable_columns` on any table (only `description`, `db_schema`, `datasource`,
`columns`, `column_types`). So the deterministic value-resolution tier
(`retrieval/dimension_vocabulary.py`) has nothing to prefetch, and the warehouse-backed
filters the model should be *handed* — offer-kind, currency, contract-kind, clearing-kind,
status, symbol — are dropped or guessed. Root cause of the dropped-filter cluster in the
report (4× symbol, 3× offerkind, 2× currency, 2× customer-type, 1× clearingkind, 1× status).

**Key shape** (from `project_config.example/schema.yaml:203-238`): per table,
`resolvable_columns` (columns allowed for value matching) and `prefetchable_columns` (subset
whose **entire distinct set is fetched** — small-cardinality dims only; large tables like
Customer/Supplier are resolvable but deliberately *not* prefetchable). Add **both** keys.

**Change** (column names verified against this `schema.yaml`, 2026-11-22):

```yaml
  OfferKind:          # schema.yaml:355 — column is `Name`
    resolvable_columns: ["Name"]
    prefetchable_columns: ["Name"]
  OfferStatus:        # schema.yaml:374 — column is `Name`
    resolvable_columns: ["Name"]
    prefetchable_columns: ["Name"]
  ContractKind:       # schema.yaml:166 — column is `PersianName`
    resolvable_columns: ["PersianName"]
    prefetchable_columns: ["PersianName"]
  ContractStatus:     # schema.yaml:197 — column is `Name`
    resolvable_columns: ["Name"]
    prefetchable_columns: ["Name"]
  ClearingKind:       # schema.yaml:147 — column is `Name`
    resolvable_columns: ["Name"]
    prefetchable_columns: ["Name"]
  Currency:           # schema.yaml:216 — column is `PersianName`
    resolvable_columns: ["PersianName"]
    prefetchable_columns: ["PersianName"]
  Symbol:             # schema.yaml:499 — mid-sized; prefetch the two display names
    resolvable_columns: ["Commodity_PersianName", "TradingSymbol"]
    prefetchable_columns: ["Commodity_PersianName", "TradingSymbol"]
  Auction_Dim.Customer:   # schema.yaml:241 — LARGE; resolvable, NOT prefetchable
    resolvable_columns: ["TypeName"]        # عمومی / حقوقی / حقیقی (3-value enum)
    # (no prefetchable_columns on purpose — see note)
```

Notes:
- **Customer is resolvable but NOT prefetchable** by design (large table; the example schema's
  own Customer/Supplier are deliberately prefetch-excluded for this reason). The 3-value
  `TypeName` enum is still resolved via `resolvable_columns` + the entity aliases in §8, so
  "مشتریان حقوقی" gets its value without a full-table scan.
- If `Symbol` prefetch is too slow on this network, keep only `Commodity_PersianName`.
- These additions trigger one bounded single-table `SELECT DISTINCT` per column at warm-up
  (stale-while-revalidate). Confirm with `scripts/verify_deployment.py` after the change.

**Expected effect:** recovers the dropped offerkind/currency/contract-kind/clearing-kind/status
/symbol filters **with no model change** — the biggest win for **Layer A** (the deterministic
diagnostic, whose `multi` recall should rise sharply) and a steadier Layer B, since the model
is now handed the exact warehouse value.

---

## 5. `examples.yaml` — rebalance to auction and fix the dialect bug (Medium)

**Current state (verified):** 20 examples — **10 auction, 10 futures**. Every futures example
is anchored "در بازار آتی" (correct *for* futures). Two real issues:

1. **Balance.** This deployment's default source is auction and the whole benchmark is
   auction; a 10/10 split dilutes the auction signal (the auction prompt shows only the 10
   auction examples, the futures prompt only the 10 futures).
2. **A T-SQL bug.** Several examples use `LIMIT` (e.g. lines 21–22, 46, 81, 106), which the
   system prompt **forbids** ("use `TOP N` instead, never `LIMIT`"). If a question is
   legitimately routed to `Future_DM` (or an auction top-N is imitated from these), the model
   emits `LIMIT`, the SQL guard rejects it, and a correction round is spent.

Note on the mis-route: because prompts are **source-scoped** (see "Why config"), the futures
examples are *already hidden* from an auction-routed question, so the examples are **not** the
cause of the 14 mis-routes — §1/§2/§3 are. This section is about auction-prompt quality and
the `LIMIT` bug.

**Change:**
- Rewrite every `… LIMIT N` to `… TOP N` / `SELECT TOP N …` (T-SQL).
- Trim to **~14 auction / 2–4 futures** (keep only futures examples that carry an explicit
  "بازار آتی/مشتقه" anchor).
- Add auction examples for the benchmark's missed patterns (table/columns verified):

  | Add an example for | Fixes (report) |
  |---|---|
  | «۵ نماد پرتقاضا به تفکیک مجموع ارزش قرارداد» → `Contract JOIN Symbol … GROUP BY Symbol.Commodity_PersianName` | 4× missing symbol + group-by-symbol (RP-047, RP-056, RP-071) |
  | «تعداد مشتریان فعال» → `COUNT(*) FROM Auction_Dim.Customer WHERE IsActive = 1` | 2× active (RP-005, RP-065) |
  | «مجموع ارزش عرضه‌های صادراتی» → `Offer JOIN OfferKind WHERE Name = 'صادراتی'` | 3× offerkind (RP-023, RP-033, RP-039) |
  | «قراردادهایی که با دلار تسویه شده‌اند» → `Contract JOIN Currency WHERE PersianName = 'دلار'` | 2× currency (RP-024, RP-036) |
  | «قراردادهای مشتریان حقوقی» → `CustomerContract JOIN Auction_Dim.Customer WHERE TypeName = 'حقوقی'` | 2× customer-type (RP-022, RP-038) |

---

## 6. `metrics.yaml` — fill the named metrics (Medium)

**Current state (verified):** still the template — `SUM(YourFactTable.TotalPrice)` placeholders
(lines 12/17/22). `metric_retriever` therefore never fires and the model infers the metric
column from `business_rules.yaml` alone, which produced the 3× wrong-metric-column misses.

**Change** (expressions copied from the *correct* `business_rules.yaml`):

```yaml
metrics:
  total_contract_value:
    aliases: [ارزش کل قراردادها, مجموع ارزش قراردادها, ارزش معاملات, مبلغ کل, total value]
    expression: "SUM(Auction_Fact.Contract.TotalPrice)"
  total_contract_volume:
    aliases: [حجم قراردادها, مجموع مقدار تطبیق سالن, حجم معاملات فیزیکی, volume]
    expression: "SUM(Auction_Fact.Contract.HallMatchingQuantity)"
  total_offer_value:
    aliases: [ارزش عرضه‌ها, مجموع ارزش عرضه‌ها, total offer value]
    expression: "SUM(Offer.OfferPrice)"
  total_offer_quantity:
    aliases: [مقدار عرضه‌ها, مجموع مقدار عرضه‌ها, total offer quantity]
    expression: "SUM(Offer.OfferQuantity)"
  contract_count:
    aliases: [تعداد قراردادها, چند قرارداد, count of contracts]
    expression: "COUNT(Auction_Fact.Contract.ID)"
  active_customer_count:
    aliases: [تعداد مشتریان فعال, مشتریان فعال, active customers]
    expression: "COUNT(Auction_Dim.Customer.ID)"
```

Keep all metrics in the **auction** source. If a futures metric is added later, its alias must
carry an explicit "آتی/مشتقه" cue (see §2 rule 1) so a generic question cannot trigger it.

---

## 7. `aliases.yaml` — add the weekday tokens (Medium)

**Current state (verified):** the four seasons already map to `["فصل", "تاریخ"]` (lines
154–157, fine). But the **weekday names appear nowhere**, so a "روزهای شنبه" question has no
alias to force the `Date` table; RP-015 scored 50% because the model re-derived
`PersianDayOfWeek` in SQL and got the off-by-one (شنبه read as 6; canonical is 1=شنبه … 7=جمعه).

**Change** — add under `synonyms`:
```yaml
  # day of week (canonical 1=شنبه … 7=جمعه — do NOT let the model recompute)
  "شنبه":     ["تاریخ", "روز هفته"]
  "یکشنبه":   ["تاریخ", "روز هفته"]
  "دوشنبه":   ["تاریخ", "روز هفته"]
  "سه‌شنبه":  ["تاریخ", "روز هفته"]
  "چهارشنبه": ["تاریخ", "روز هفته"]
  "پنجشنبه":  ["تاریخ", "روز هفته"]
  "جمعه":     ["تاریخ", "روز هفته"]
```
And in `system_prompt.md` (§2) add: «روزی که به‌صورت `PersianDayOfWeek` تشخیص داده شده
مستقیماً در `WHERE` استفاده شود و عدد آن بازمحاسبه نشود.»

(The English `trade`/`value`/`price` tokens on lines 186–190 are harmless once the futures
examples are source-scoped away — no change needed there.)

---

## 8. `entities.yaml` — add value aliases for the dropped-filter dims (Medium)

**Why:** customer-type (حقوقی/حقیقی/عمومی), offer-kind (صادراتی/داخلی) and active
(فعال/غیرفعال) filters are resolved by the *model* (Customer is not prefetchable, §4). Giving
the entity aliases the value surface forms improves the chance the right `WHERE` is produced
even before §4's `resolvable_columns` lands.

**Change** (add to the existing `aliases:` lists; keys verified in this `schema.yaml`):
```yaml
  Auction_Dim.Customer:
    aliases: [مشتری, مشتری‌ها, /* existing */ مشتری حقوقی, مشتری حقیقی, نوع مشتری, فعال, غیرفعال]
  OfferKind:
    aliases: [نوع عرضه, /* existing */ عرضه صادراتی, عرضه داخلی, صادراتی, داخلی]
  ContractKind:
    aliases: [نوع قرارداد, /* existing */ نقدی, نسیه, سلف]
```

---

## 9. `session_policy.yaml` — hygiene only (Low)

**Verified:** the `options` list (lines 32–52) already contains **all 21** auction halls plus
"All rings" — it is complete, so **no ring is missing** (re-confirmed 2026-11-22). Only
cosmetic items:
- header comment still says "TEMPLATE (copy to …)"; mark it live.
- `default_label: "All rings"` / `clarification_prompt: "Which ring do you mean?"` are
  English; if the UI is Persian-first, consider `"همه رینگ‌ها"` / «رینگ موردنظرتان کدام است؟».
  (Backend matching uses the `options` list, not the label, so this is display-only — check
  `tests/web_ui/` before changing.)

---

## 10. What NOT to change

- **`business_rules.yaml`** — the per-fact value/volume column rules are correct and were the
  model's best guide on the 86 well-specified cases. (Used verbatim for §6.)
- **`relationships.yaml`** — join paths resolved correctly in every case that reached SQL
  generation; no finding implicates them.
- **`schema.yaml` structure** (table/column descriptions, the mixed bare/qualified keys) —
  complete and matches the live DB. Only *add* `resolvable_columns`/`prefetchable_columns`
  per §4.
- **`datasources.yaml` `Future_DM` keywords** — already specific; leave them (§3).

---

## Expected impact (in order of applying)

| Step | Change | Predicted effect on the 100-case benchmark |
|---:|---|---|
| 1 | Fill `retrieval_hints.yaml` (§1) | Largest mis-route reduction — force-includes the auction facts and fixes their fact-classification, overriding the TF-IDF pull to futures |
| 2 | `system_prompt.md` domain rule + delete generic schema (§2) | Model-side tie-break to auction; removes contradictory-schema noise; also fixes weekday re-derivation |
| 3 | `datasources.yaml` auction keywords (§3) | Makes keyword-routing deterministic for auction-worded questions |
| 4 | `resolvable/prefetchable_columns` (§4) | Recovers the dropped kind/currency/status/symbol filters **with no model change**; biggest Layer A win |
| 5 | `examples.yaml` rebalance + `TOP` fix (§5) | Steadier auction prompt; removes `LIMIT`-triggered correction rounds on futures questions |
| 6–8 | metrics / aliases / entities | Long-tail: metric column, weekday, customer-type/offer-kind |

Steps 1–3 target the **14 mis-routes** (and the weak `ambiguous` category, ≈58% → est. low-90s,
on par with `simple`). Steps 4–8 recover the dropped filters and metric/grouping misses.
After each step, re-run the benchmark (`--no-cache`) and `make_report.py`; the ambiguous-vs-
simple gap closing is the clearest single signal that routing is fixed.
