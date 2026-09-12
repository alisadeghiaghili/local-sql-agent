# Design invariants — what the 5.0 redesign may not break

> **Companion to** [`DESIGN.md`](DESIGN.md). That document decides what the UI
> should become. This one fixes what it may not cost us on the way.
>
> **Status:** policy · **Applies to:** every train in [`IMPLEMENTATION.md`](IMPLEMENTATION.md)

`DESIGN.md` was written against `main @ 6ee96d7`. Between that commit and this
one, a security audit landed nineteen fixes, four of them High severity, and
several live in exactly the files Trains A–C rewrite: `web/admin/main.js`,
`web/index.html`, `web/admin/index.html`, `api/admin_write_routes.py`.

A redesign that reverts a security fix is not a redesign. It is a regression
with better spacing. This document names what must survive, and adds the three
rules the original policy asserts but does not make enforceable.

---

## 1. Security invariants (non-negotiable)

Every one of these is covered by a test under `tests/security_audit/`. If a
redesign commit turns one red, the redesign is wrong — not the test.

### 1.1 The admin panel's XSS chain stays closed

Finding 2 of the audit was a stored XSS that escalated `operations` to
`security`. It needed breaking in four places, because each link was
individually reasonable:

| Invariant | Where | Why it is not optional |
|---|---|---|
| `escapeHtml` encodes `& < > " '` | `web/admin/main.js` | The `textContent`→`innerHTML` idiom encodes what matters in a *text node*. Two call sites put its output in an *attribute*, where the quote it skips is the only character that matters. |
| Row identity set via `el.dataset`, never string-interpolated into `data-*="…"` | `web/admin/main.js` | An escaper is one refactor from being bypassed. A real element's `dataset` cannot produce an attribute injection at all, whatever the value holds. |
| `principal_id` carries `pattern=r"^[A-Za-z0-9._-]+$"` | `api/admin_write_routes.py` | A token with a double quote in it is a mistake wherever it lands — an attribute, a log line, a CSV cell. |
| Both pages carry a `<meta>` CSP whose `script-src` excludes `'unsafe-inline'` | `web/index.html`, `web/admin/index.html` | `SecurityHeadersMiddleware` never sees these files: a static server delivers them, not FastAPI. A header the API sets cannot reach a page another origin serves. |

`DESIGN.md` §9.2 already requires the fourth one (*"CSP: `script-src 'self'` —
no inline handlers"*). It is recorded here because the branch that states the
rule does not yet implement it.

### 1.2 What this costs the redesign: nothing

Checked, not assumed, against `feature/ui-design-system`:

* Its `web/index.html` and `web/admin/index.html` contain **zero** inline
  `on*=` handlers, so adopting the CSP breaks nothing.
* The one `innerHTML` in its new `web/js/sql-display.js` is safe: `textContent`
  is set first as the baseline, and Prism escapes its input before tokenising.
  Keep that shape — `textContent` first, highlighter second, plain text on any
  failure.

The redesign is CSP-compatible today. It simply has not opted in.

### 1.3 Rendering rule that generalises all of the above

> **Build DOM, not HTML strings, for anything carrying a value this process
> did not author.** Analyst questions, warehouse rows, key identifiers, model
> output. `createElement` + `textContent` + `dataset`.

`web/js/render/table.js` already does this and was the one surface the audit
found unexploitable. It is the pattern, not the exception.

---

## 2. Bidirectional text (RTL) — rules, not vigilance

`DESIGN.md` §8 says "Logical properties only; identifiers isolated LTR". True
and insufficient: both bugs below passed that rule and still rendered wrong.
They were found by measuring a running page, never by reading the CSS.

### 2.1 A forced direction applies to the *value*, never to its prose

**Measured defect.** The API-key field is `direction: ltr`, which is right — an
API key is ASCII and must not reorder. Its placeholder is Persian:

| | Read right-to-left, as a Persian reader reads |
|---|---|
| Today | `خود را وارد کنید API کلید` |
| Correct | `کلید API خود را وارد کنید` |

The Latin run `API` inside Persian prose is what reorders. A pure-Persian
placeholder (the admin panel's `کلید مدیریتی`) does **not** reorder — it only
misaligns. The two are different severities and must not be lumped together.

**Rule.** When an input's value needs `direction: ltr`, the placeholder is
styled back to the document direction:

```css
.some-input { direction: ltr; }
.some-input::placeholder { direction: rtl; text-align: right; }
```

Or flip on content, which also fixes alignment while typing:

```css
.some-input { direction: rtl; }
.some-input:not(:placeholder-shown) { direction: ltr; }
```

### 2.2 An off-canvas panel hides toward the edge it lives on

**Measured defect.** In RTL the conversation drawer sits flush to the **right**
edge. The rule pushed it left:

```css
[dir="rtl"] .sidebar { transform: translateX(-100%); }
```

Measured at a 375px viewport:

| transform | left | right | visible |
|---|---|---|---|
| none (open) | 75 | 375 | 300 |
| `translateX(-100%)` | −225 | 75 | **75** |
| `translateX(100%)` | 375 | 675 | **0** |

`-100%` is the element's own width, not the distance to the far edge. With a
300px panel in a 375px viewport it lands 75px short, and those 75px covered the
header and the Ask button — which is why the page *looked* like it had a
dozen clipping bugs when it had exactly one.

**Rule.** A panel anchored to the inline-end edge hides with
`translateX(100%)` under RTL and `translateX(-100%)` under LTR. Prefer a single
direction-agnostic declaration where support allows. Any panel that slides must
have a test asserting **zero** visible width when closed, at 375px.

### 2.3 Test at the direction the product ships in

Both defects are invisible in an LTR browser. Every layout assertion runs
against `dir="rtl"`, because that is what every analyst sees.

---

## 3. Accessibility — the bar `DESIGN.md` sets and the code misses

§8 requires *"Tap targets ≥ 44×44 on touch viewports"*. Measured on the
shipping UI: the interpretation toggle's effective target is **81×20**. The
mockup reproduces the same `<label><input type="checkbox">` pattern with no
sizing, so it would ship the same miss.

**Rule.** Every control's hit area is at least **24×24 CSS px** (WCAG 2.5.8
minimum) and **44×44** inside a touch breakpoint. For a checkbox this means
the wrapping `<label>` carries the padding — a 13px native checkbox is never
the target on its own.

Stated as a floor rather than the policy's single 44 figure because 24 is the
conformance line and 44 is the comfort line; a rule nobody can meet at desktop
density gets dropped, and then neither number holds.

---

## 4. Theme — the decision `DESIGN.md` left implicit

§14b records "dark-mode mockup missing" as a gap. It is larger than a missing
drawing: all three mockups set `--bg: #f4f6fa`, a light ground, while the
shipping UI renders dark. Adopting the mockups as drawn changes the product's
default appearance without anyone deciding to.

**Decision to make explicit before Train B lands:**

| Option | Consequence |
|---|---|
| Follow the system (`prefers-color-scheme`), as today | No visible change for existing analysts; mockups are the light half |
| Light default | A deliberate, announced change; needs the dark half drawn before it ships |
| Dark default | Mockups do not represent the product and must be redrawn |

Whichever is chosen, `tokens.css` redefines **the same** custom properties per
theme — never a colour that exists only inside one media query. That rule is
already law in `web/styles/style.css` and survives unchanged.

---

## 5. Breakpoints

§14b lists "375px" as undrawn. The shipping CSS has exactly one breakpoint, at
640px, so 768–1024 tablets get the desktop chrome the policy already calls a
junk drawer.

**Rule.** Every layout change is verified at **375 / 768 / 1280**, with
`dir="rtl"`, and the assertion is *no element extends past the viewport and the
document does not scroll horizontally* — measured, not eyeballed. The drawer
bug above produced a page that looked broken in a dozen places and measured as
one root cause; eyes rank symptoms, measurement finds causes.

---

## 6. Attribution

`IMPLEMENTATION.md` states "No co-author trailers. No AI attribution."

Commits on `security/audit-remediation` carry a `Co-Authored-By` trailer,
because the tooling that produced them is required to add one. The two branches
therefore disagree, and a merge inherits both conventions.

This is recorded, not resolved: how the repository owner attributes their own
work is their call, and rewriting those trailers is a history edit only they
should make.

---

## 7. How this document is enforced

| Invariant | Test |
|---|---|
| §1.1 all four layers | `tests/security_audit/test_admin_panel_xss.py` |
| §1.1 survives the redesign | `tests/security_audit/test_redesign_preserves_fixes.py` |
| §2.1 placeholder direction | `tests/web_ui/test_web_ui_rtl_layout.py` |
| §2.2 drawer fully hidden | `tests/web_ui/test_web_ui_rtl_layout.py` |
| §3 tap targets | `tests/web_ui/test_web_ui_rtl_layout.py` |
| §5 breakpoints | `tests/web_ui/test_web_ui_rtl_layout.py` |

A policy with no test is a preference. These have tests.

---

## 8. Failure has an anatomy, or it has no design

`DESIGN.md` §2 lists "Failure — honest empty/error, with a next action" among
the moments that matter. §5.2 then specifies only **"a successful turn"**, and
none of the three mockups draws a failed one. The moment a user most needs
design is the only one that has none.

Failure is not an edge case here. Guard rejection, a denied column, model
timeout, truncated model output and zero rows are all implemented paths with
their own error codes.

**Rule — every failure state renders three things, in this order:**

1. **What happened**, in the analyst's terms, not the system's.
2. **Why**, when the reason is knowable and useful.
3. **The next action**, as a control they can press.

An error code alone is not a failure state. It may appear, small, beside the
prose — never instead of it.

| Path | What happened (leading sentence) | Next action |
|---|---|---|
| Guard: denied column | The query did not run — a column it needed is restricted for your account. **Not** "no results" | Ask without that column · Request access |
| Guard: forbidden statement | Refused before running: the generated query tried to change data | Rephrase · See the SQL |
| `MODEL_UNAVAILABLE` | Your question was kept. The engine could not be reached — a system problem, not your question | Try again · Notify admin |
| `LLM_OUTPUT_TRUNCATED` | The model stopped before finishing the query | Retry shorter · (operator: raise the cap) |
| Zero rows | The query ran correctly and nothing matched. The likeliest cause is one of the assumptions | Offer each assumption as an editable chip |
| Execution error | The database refused the query | Show the SQL · Report |

The distinction that carries the most weight is the first row's: **"did not
run" and "returned nothing" must never look the same.** An analyst who reads a
guard rejection as an empty result concludes the data does not exist, and acts
on it.

Copy discipline (applies to every string above):

* Name the subject. "The query did not run", not "Error occurred".
* Never blame the user for a system fault; never absolve the system for a
  user-correctable one.
* The button says the action, not `OK`. "Ask without that column", not "Retry".
* Keep the machine-readable code and the `request_id` visible but subordinate —
  an operator needs them; the sentence is not for them.

---

## 9. What a user switches on is never hidden

`DESIGN.md` §5.2 item 9 places the interpretation inside the collapsed details
drawer. §14b already records burying assumptions there as a product error and
corrects it. The same error was then made one item later, and not caught.

This case is worse, because interpretation is not merely important — it is
**opted into**. The analyst ticks a box whose label states its cost ("up to
twenty rows to the model"). Hiding the thing they deliberately asked for is a
contradiction, not a hierarchy.

> **Rule.** Anything the user explicitly enabled renders in the main flow.
> Progressive disclosure applies to what the product decided to show, never to
> what the user decided to request.

Corollary for the drawer's remaining contents: pipeline timings and the LLM
status strip are shown *by the product*, so they may collapse. If a future
setting lets an analyst turn the LLM strip on deliberately, it leaves the
drawer by this rule.

---

## 10. Chart emphasis is a lightness job, not a hue job

The line chart splits its series into a "context" segment and a "focus"
segment and distinguishes them by hue. Measured with the `dataviz` skill's
validator against the real tokens:

| | normal vision ΔE | deuteranopia ΔE | focus↔context contrast |
|---|---|---|---|
| Light | 11.0 | 5.3 | 1.27 |
| Dark | 11.9 | **2.2** | **1.03** |

The target is ΔE ≥ 8 and a hard floor of 15 for normal vision. A contrast ratio
of 1.03 means the two lines are the *same perceived brightness*: in dark mode
the distinction the chart is built on does not exist for a colour-blind
analyst, and is marginal for everyone else. The only secondary encoding present
was a stroke-width difference of 2.5 against 3.

The deeper error is the encoding choice. **Focus versus context is emphasis,
not category.** Emphasis is carried by lightness and weight; hue carries
identity. Two hues at the same lightness is the wrong tool, which is why a
categorical validator flags it.

**Rule.** Where the chart means "this part matters more", separate by
lightness and stroke weight and keep the hue. Reserve hue changes for marks
that mean *different things*, and run the validator on any categorical set.

Corrected pair, keeping brand teal as the focus:

| | focus | context | contrast | weights |
|---|---|---|---|---|
| Light | `#0d9488` | `#b8c4d4` | 2.12 | 1.5 → 3 |
| Dark | `#14b8a6` | `#3d4c63` | 3.50 | 1.5 → 3 |

Four redundant encodings then carry the distinction — lightness, weight, the
focus point, the direct label — so hue is no longer the sole signal.

---

## 11. Accessibility, measured

`DESIGN.md` §8 sets a bar. Nobody had checked the shipping tokens against it.
Computed against `web/styles/tokens.css`:

| Check | Light | Dark | WCAG |
|---|---|---|---|
| Body text on card | 14.68 ✅ | 13.98 ✅ | 1.4.3 (4.5) |
| Secondary text on card | 4.76 ✅ | 6.64 ✅ | 1.4.3 |
| Secondary text on page | **4.40 ❌** | 7.30 ✅ | 1.4.3 |
| Primary action label | **3.74 ❌** | **2.49 ❌** | 1.4.3 |
| Ask-field boundary | **1.23 ❌** | 1.34 ❌ | 1.4.11 (3.0) |
| Focus indicator present | ✅ | ✅ | 2.4.7 |

**The primary action fails in both themes, and it is the most-pressed control
in the product.** White on `--teal #0d9488` is 3.74 light and 2.49 dark, and a
15px bold label is not "large text" (that starts at 18.66px bold).

This row was wrong in the first draft: it carried 6.84 for dark and called it a
pass. 6.84 is teal as *text on a card* — a different pairing. A button is a
white label on a teal *fill*, and measuring the wrong pair is exactly how a
control ships failing while a table says it passed. Corrected here after the
spec test caught it.

The ask field's boundary fails 1.4.11 twice over: its border against the card
is 1.23 and its fill difference is 1.05, so neither identifies the control at
the required 3:1. Decorative hairlines may stay light; an **interactive**
boundary needs its own token.

Secondary text is the marginal one: it passes inside a card (4.76) and fails on
the page ground (4.40). `#5b6b81` measures 5.02.

Both themes carry failures; the light theme carries more. With §12's decision
below, both ship.

**Rule.** Contrast is computed, never judged. Any new or changed token pair in
`tokens.css` is checked against 4.5 (text), 3.0 (interactive boundary and
non-text) before it lands.

---

## 12. Theme — decided

The question §4 raised is answered: **follow the system.**
`prefers-color-scheme` decides, as it does today. No analyst's habits change,
and the mockups are understood as the light half of a two-theme product rather
than as a proposal to make light the default.

This makes §11's light-mode failures live for roughly half the users rather
than none, which is why they are listed as defects and not as theme-selection
notes.
