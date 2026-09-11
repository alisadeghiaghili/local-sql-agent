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
