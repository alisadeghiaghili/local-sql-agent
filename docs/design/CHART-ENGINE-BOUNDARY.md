# The chart engine — where its boundary runs, and why there is one

> **Status:** specification · **Implements:** the recommendation decision
> **Companion to** [`DESIGN-INVARIANTS.md`](DESIGN-INVARIANTS.md) §10 (chart emphasis).
>
> This document fixes the **interface** of a chart-recommendation engine and the
> reasoning behind each line of it. It does not specify the chart vocabulary;
> §7 deliberately defers that to one form at a time.

---

## 0. The incident this is built around

A real result — ten customers ranked by traded volume — rendered as a **line
chart** with company names along the x axis, under the headline
«مقدار … رو به کاهش بود» ("the amount was declining"). Nothing was declining.
The rows arrive sorted by value, so a *ranking* drew the shape of a downward
trend, and the headline then described that shape as though time had passed.

The cause was one unconditional branch in `chooseFramings()`, whose own stated
reason — «سنجه در طول یک توالی است» ("the measure is along a sequence") — was an
assertion the function never tested.

That is the most consequential class of chart mistake available: **it does not
mislabel the answer, it invents a different one.** An analyst who reads it
concludes something about the world that the data never said.

It was found and closed in an afternoon, because the decision was deterministic
and therefore testable. `tests/web_ui/test_web_ui_chart_form_choice.py` now pins
it in five scenarios.

**Hold that fact next to the design question below.** It is the whole argument.

---

## 1. Decision — borrow A2UI's catalog, reject its agent authority

[A2UI](https://a2ui.org/) has one idea worth taking: the **host declares a
catalog** of components it can render, and the agent composes a tree *from that
catalog only*. The output is declarative data, never executed code. That is what
makes an A2UI surface portable across hosts and safe to render.

**Taken:** the catalog. The host application declares which chart forms exist
and what each one requires. The engine selects from that declaration and writes
down why. A host with only bar and line gets a selector that knows only bar and
line, with no change to the engine.

**Rejected:** the model as the one who chooses.

The reason is §0, stated plainly:

> We fixed a bug today in which the wrong choice of chart form manufactured a
> false narrative. That bug was deterministic and testable, so it was found and
> closed. Hand the same decision to a model and that error class becomes
> non-deterministic and untestable.

This is not a claim that a model would choose worse on average. It is a claim
about **what happens when it chooses wrong**: there is no failing test to write,
no line to fix, and no way to prove the fix held. A wrong chart is a wrong
statement about the warehouse, made in the analyst's name.

> *همان استدلال قابلیت‌حمل A2UI، منهای LLM.*

---

## 2. Decision — a separate product, yes in principle, not yet

The objection is to the timing, not the idea.

> *مخالفتم با ایده نیست، با زمان‌بندی است.*

A recommendation engine that has never selected a chart for anyone has no
evidence about which signals matter. Extracting it now would freeze today's
guesses into a published interface and make each of them expensive to correct.

**So: build it inside this repository, behind a boundary strict enough that
extraction later is a move, not a rewrite.** §3–§6 are that boundary. Every rule
in them is a rule a standalone package would have to satisfy anyway; adopting
them now costs nothing and buys the option.

The test in §8.1 is what keeps this honest — an import of a DOM symbol or a
sibling renderer turns it red on the commit that adds it, not six months later
when someone tries to extract the module and discovers it has grown roots.

---

## 3. The missing layer: features → **job** → form

The code today jumps straight from data features to a chart form:

```
columns/types/row-count  ──────────────────────────────▶  line | bar | split-bar | pie
```

That jump is where the defect lived. "Two columns, one numeric, five rows"
genuinely does not determine whether a line is appropriate — the missing term is
what the analyst is **asking**.

```
features  ──▶  analytical job  ──▶  form
```

The engine's real output is the middle term. The form follows from it.

**The job vocabulary** (closed set; adding to it is a decision, not an
implementation detail):

| Job | The question it answers | Sequence required? |
|---|---|---|
| `rank` | Which is largest / smallest? | no |
| `trend` | How did it move over time? | **yes** |
| `comparison` | How do these two periods or groups differ? | for periods |
| `composition` | What is each part's share of the whole? | no |
| `distribution` | How are the values spread? | no |
| `correlation` | Do these two measures move together? | no |
| `deviation` | How far is each from a reference? | no |

The line-over-customer-names defect is exactly a `rank` misread as a `trend`.
Naming the job makes that a *type error* rather than an aesthetic preference,
and a type error is something a test can state.

---

## 4. Input contract — the signals the engine may read

Every field below is already on the client. None requires a backend change.

| Signal | Source | What it discriminates |
|---|---|---|
| `columns[].name`, `columns[].type` | `session/models.py::ResultColumn` | The existing sequence gate (`"datetime"`, calendar words) |
| `rows`, `row_count`, `truncated` | `TurnResult` | Legibility ceilings; a truncated result must never be described as a whole |
| `sql` | `Turn.sql` | **Unused today, and the strongest signal available** |
| `guard.tables_touched` | `GuardVerdict` | Which dimension the labels come from |
| `resolved_question` | `Turn.resolved_question` | The analyst's own words, after resolution |
| the host catalog | the caller | Which forms exist at all |

### 4.1 Why `sql` is the signal worth adding first

`GROUP BY` names the dimension the measure is aggregated over. `ORDER BY <measure> DESC`
with a `TOP n` is a **ranking, stated by the query itself** — the engine would
not need to infer from column names what the SQL already says outright. The
motivating defect was a `SELECT TOP 10 … ORDER BY SUM(...) DESC`: the query
declared it was a ranking, and nothing read it.

Parsing constraint: a substring scan for `ORDER BY`, not a SQL parser. The engine
runs in a browser with no build step (`web/README.md`: no `package.json`, no
`node_modules`), and a wrong parse must degrade to "no signal", never to a wrong
signal. `security/sql_guard.py` does the real parsing server-side, where being
wrong is a security problem rather than a presentation one.

### 4.2 What the engine may never read

- the DOM — no `document`, no `window`, no element, in any code path
- any module under `web/js/render/`
- network, storage, timers, `Date.now()`

The last one is not pedantry: a selector whose output depends on the clock cannot
be pinned by a test, and §8 is the only thing standing between this engine and
§0 happening again.

### 4.3 A wiring fact, verified

`web/js/render/turn.js:227` calls `renderResult(turn.result, {…})`. It has the
whole `turn` in scope and passes only `result`. So `sql`, `resolved_question` and
`guard` are **not unavailable — they are dropped one layer down.** Threading them
through is an additive change to the existing `opts` object, not a rewrite.

---

## 5. Output contract — a recommendation is a reason, or it is noise

The engine returns an **ordered list of candidate framings, plus the rejected
ones with their reasons.** Rejections are part of the output, never silently
dropped: `chooseFramings` already shows the rejected pie framing with its reason,
and that is the behaviour to keep. An analyst looking for an option they cannot
find deserves an answer, and an engine that shows its rejections is one a reader
can argue with.

Each framing carries:

| Field | Meaning |
|---|---|
| `form` | An id **from the host catalog** — never a form the host did not declare |
| `job` | The §3 job this framing serves |
| `reason` | Why this form, for this job, on this data — in the analyst's language |
| `rejected` / `rejectionReason` | Present and explained, for forms that do not fit |
| `spec` | A declarative description of the mark, encodings and emphasis |

`spec` is data, not DOM — the Vega-Lite lesson, and the A2UI one. The renderer
consumes it; the engine never builds an element. That single rule is most of what
makes §2's extraction a move rather than a rewrite.

### 5.1 The signature, locked

Fixed here so the test and the implementation cannot disagree about it. This is
what `tests/web_ui/run_chart_engine_boundary.mjs` drives, and it is not open for
reinterpretation during implementation:

```js
// web/js/chart-engine/recommend.js
export function recommend(input, catalog) -> { framings: Framing[] }

input = {
  columns:          [{ name, type }],   // session/models.py::ResultColumn
  rows:             [ {...} ],
  rowCount:         number,
  truncated:        boolean,
  sql:              string | null,      // Turn.sql — §4.1
  resolvedQuestion: string | null,      // Turn.resolved_question
  tablesTouched:    string[],           // GuardVerdict.tables_touched
}

catalog = { forms: [{ id, jobs: string[],
                      requiresSequence?: boolean,
                      maxCategories?: number }] }

Framing = {
  form:   string,             // an id from catalog.forms, never invented
  job:    string,             // from the §3 closed set
  reason: string,             // only what the engine actually tested
  rejected: boolean,
  rejectionReason?: string,   // required when rejected
  spec?: object,              // declarative; required when not rejected
}
```

Three behaviours the signature alone does not state, all asserted by the harness:

* **Purity.** `recommend` does not mutate `input`. A selector that sorts its
  caller's rows in place makes every later reading of that turn depend on
  whether a chart happened to be recommended first.
* **Serialisability.** The return value survives a JSON round-trip unchanged.
  A value that does not — a DOM node, a function, a `Symbol` — is a §4.2
  violation wearing a disguise, and this is the cheapest way to catch one.
* **Truncation honesty.** A `truncated` result is never given the `composition`
  job. Shares of a whole, computed over a prefix of the rows, are not shares of
  the whole — the same class of error as §0, in a different costume.

**The `reason` string is load-bearing, not decoration.** The §0 defect shipped
with a reason attached that asserted something the code never checked. So:

> **Rule.** A reason may only state what the engine actually tested. If the
> reason says "sequence", a sequence check ran. A reason that outruns its check
> is how the original defect survived several readings of the file.

---

## 6. Emphasis is inherited, not reinvented

`DESIGN-INVARIANTS.md` §10 already decides that focus-versus-context is
**emphasis** (lightness and weight), not **category** (hue), with measured ΔE
and contrast figures behind it. The engine's `spec` expresses emphasis in those
terms and does not introduce a second colour policy.

One honest caveat for whoever implements this: §10 cites the `dataviz` skill's
`validate_palette.js`. **That tool is not installed on this machine** (checked).
Any new colour claim must therefore be computed in the test itself, the way
`tests/web_ui/test_web_ui_contrast_and_failure.py` already computes WCAG ratios
inline — "the arithmetic is here rather than in a comment, and it runs on every
change".

---

## 7. Sequencing — the current vocabulary first, then one form at a time

**Step 1.** Rebuild today's selection (line, bar, split-bar, rejected pie) behind
the §4/§5 boundary. **No new chart forms.** The observable behaviour of the UI
does not change, and `test_web_ui_chart_form_choice.py` plus
`test_web_ui_result_shapes.py` must stay green throughout — they are the proof
that the refactor preserved the fix.

**Step 2 onward.** One form per change, each with its own test stating when it
**must** be offered and when it must **not** be.

> *اگر همه را با هم بزنیم، وقتی چیزی غلط پیشنهاد شد نمی‌فهمیم تقصیر موتور است یا
> آن فرم خاص.*

Candidate forms, in the order their evidence is strongest — **not authorised
here**, each needs its own decision:

| Form | Job | Why it is wanted |
|---|---|---|
| grouped / stacked bar | `comparison`, `composition` | `determineShape` gives a three-column result **no chart at all** today (`table.js:79` requires exactly 2 columns) |
| slope chart | `comparison` | Two periods, many entities — the case a line chart currently mangles |
| dot plot / lollipop | `rank` | Long Persian category labels, where bars waste ink |
| histogram | `distribution` | Currently unrepresentable |
| scatter | `correlation` | Two measures — no shape for this exists |

The three-column gap is the single largest one: a perfectly ordinary
`CustomerName, Ring, TotalValue` result renders as a bare table.

---

## 8. What the tests pin

Written before the implementation, and red against `HEAD` when written.

**8.1 The boundary itself.** The engine module imports nothing from
`web/js/render/`, references no DOM global, and is loadable in bare Node with no
DOM shim at all. This is the test that keeps §2's promise; it fails on the commit
that breaks it.

**8.2 Catalog obedience.** Given a catalog, the engine never returns a form
outside it, and degrades honestly when the catalog cannot serve the job — it says
so rather than substituting a form that fits badly.

**8.3 Determinism.** The same input yields byte-identical output across runs and
across process boundaries.

**8.4 Reasons are checked claims.** For every offered framing, any sequence claim
in its reason implies a sequence test actually ran (§5's rule, and §0's defect).

**8.5 The §0 regression, at the new layer.** A ranking is never assigned the
`trend` job. This duplicates `test_web_ui_chart_form_choice.py` one layer down,
deliberately: that test guards the current renderer, this one guards the engine
that replaces it, and the overlap is what makes the migration safe.

---

## 9. Non-goals

- **No LLM in the selection path.** §1.
- **No new chart forms in the first change.** §7.
- **No extraction to a separate package yet.** §2.
- **No new runtime dependency.** `web/` ships no build step by design; a
  recommendation engine that requires one has broken the boundary it claims.
- **No change to what the analyst sees, in step 1.** A refactor that also changes
  behaviour cannot be reviewed, because nothing distinguishes an intended change
  from a regression.

---

## 10. The standing warning

> *موتوری که پیشنهاد می‌دهد، می‌تواند اشتباه هم پیشنهاد بدهد — و هرچه واژگانش
> بزرگ‌تر، راه‌های اشتباه بیشتر.*

An engine that recommends can recommend wrongly, and every form added to its
vocabulary adds ways to be wrong. Nothing in this document removes that risk;
what it does is bound it:

1. **Determinism** — a wrong recommendation is reproducible, so it is fixable.
2. **Written reasons** — a wrong recommendation states its own reasoning, so it
   is arguable rather than merely disliked.
3. **Visible rejections** — what was considered and refused is on screen.
4. **A test per form** — saying when it must be offered *and when it must not*.

The fourth is the one that is easy to skip and the one that matters. A test
asserting a form appears where it fits proves only half of what §0 needed.
