# Design Policy — Local SQL Agent UI

> **Status:** proposal · **Target release:** 5.0.0 · **Owner:** product + engineering  
> **Supersedes:** ad-hoc styling in `web/`, `webapp/`, `site/` as three independent surfaces  
> **Does not supersede:** `docs/admin-panel-architecture.md` (ops contract), API contract v2

This document is the single source of truth for every pixel this project ships.
It is written the way the rest of the repository is written: decisions and the
reasons behind them, not a style gallery.

---

## 1. Why this exists

Three front-ends currently share one engine and no identity:

| Surface | Role today | Problem |
|---|---|---|
| `web/` | Conversational analyst UI + admin | Strong tokens, weak IA, demo chrome in production |
| `webapp/` | Flask single-shot with auth | Parallel product model, foreign palette, full page reload |
| `site/` | Marketing / landing | Foreign palette, CDN fonts, minified CSS, wrong hero SQL |

A product that cannot be recognised across surfaces is not a product. This
policy freezes **one** design language and **one** ship vehicle.

---

## 2. Service design frame

The service is not "a chat box over SQL". It is a **governed analysis loop**:

```
ask → clarify → generate → guard → execute → interpret → remember → audit
```

Every UI decision must make one of those eight steps clearer, safer, or faster.
Anything that does not is decoration and is removed.

### Actors

| Actor | Primary surface | Job to be done |
|---|---|---|
| Analyst | `web/` (product) | Get a trustworthy number or table, fast, in Persian or English |
| Operator / admin | `web/` `/admin` | Know if the deployment is healthy; fix keys, cache, feedback |
| Visitor / buyer | `site/` | Understand the pitch in under 60 seconds; reach the repo |
| Future: API client | none | Out of scope for this policy |

### Moments that matter

1. **First open** — health is visible; identity state is obvious; no mode switch.
2. **Ask** — one primary action; sample stories only in demo.
3. **Doubt** — assumptions, guard verdict, and truncation are first-class, not footnotes.
4. **Failure** — honest empty/error, with a next action.
5. **Return** — session list restores context without re-asking.
6. **Ops firefight** — admin leads with what is broken, not with equal cards.

---

## 3. Product surfaces (after 5.0)

| Path | Status | Decision |
|---|---|---|
| `web/` | **Canonical product UI** | Analyst + admin. Owns tokens, icons, components. |
| `webapp/` | **Frozen → removed** | Auth moves to the API (`/v2` + API keys already exist). Flask UI is not extended. |
| `site/` | **Marketing only** | Same brand hues, same type voice, self-hosted fonts, unminified CSS. |
| `docs/design/` | **New** | This policy + token reference + mockups. |

There is one product UI. Auth is an API concern, not a second frontend.

---

## 4. Design tokens

Tokens live in `web/styles/tokens.css` and are the only place colours, radii,
shadows, and type sizes are defined. Light and dark redefine the **same**
custom properties — never invent a colour that exists only inside a media
query (already true in `web/styles/style.css`; that file's header is law).

### 4.1 Brand vs status

Two colour axes, never mixed:

- **Brand** — chrome, primary actions, focus, identity of the product.
- **Status** — a fact about the system right now (good / warn / critical / neutral).

Source-of-assumption colours remain a **third** axis (question / session /
policy / memory / default). Chart focus-vs-context is a **fourth**, narrow axis.

### 4.2 Palette (light)

| Token | Hex | Role |
|---|---|---|
| `--navy` | `#0e2a47` | Deep chrome, question bubbles, admin mark |
| `--navy-2` | `#14375c` | Elevated navy |
| `--navy-3` | `#1b436e` | Navy interactive |
| `--teal` | `#0d9488` | Primary brand, primary action |
| `--teal-d` | `#0b7a70` | Brand hover / emphasis |
| `--teal-l` | `#e7f6f2` | Brand soft fill |
| `--bg` | `#f4f6fa` | Page ground |
| `--card` | `#ffffff` | Surface |
| `--card-2` | `#f8fafc` | Nested surface |
| `--border` | `#e2e8f0` | Hairline |
| `--ink` | `#1f2937` | Body text |
| `--muted` | `#64748b` | Secondary text |

Status and source tokens stay as they are today in `web/styles/style.css`
(they are already correct). Dark mode values likewise — copy from that file.

### 4.3 Type

| Role | Family | Notes |
|---|---|---|
| UI / body | `Vazirmatn`, `Segoe UI`, Tahoma, sans-serif | Persian-first; self-hosted in `web/styles/fonts.css` |
| Mono / SQL / ids | `Consolas`, `Cascadia Code`, monospace | Always `dir="ltr"` islands |
| Marketing display | same stack as product | Site may keep a Latin display face **only** if it falls back cleanly and is self-hosted |

No Google Fonts. No third-party CDN. The product's pitch is "no cloud
dependency"; a font request to Google is a brand lie.

### 4.4 Scale

| Token | Size |
|---|---|
| `--fs-xs` | 11px |
| `--fs-sm` | 12.5px |
| `--fs-md` | 14px |
| `--fs-lg` | 15px |
| `--fs-xl` | 18px |
| `--fs-2xl` | 21px |
| `--radius` | 12px |
| `--radius-sm` | 8px |
| `--radius-pill` | 999px |

Spacing uses an 4px base: 4 / 8 / 12 / 16 / 20 / 24 / 32.

---

## 5. Information architecture

### 5.1 Analyst chrome

**Top bar (fixed height ~56px):**

```
[ brand-mark ] [ product name          ]     [ health · one control ] [ user menu ]
```

| In top bar | Out of top bar |
|---|---|
| Brand | Mode switch (simulated) — URL param / debug only |
| One health control (opens details) | Three always-on pills |
| User menu: API key, theme, memory, about | Inline password field |
| | Backend base URL (already moved to config.js — stays) |

**Body:**

```
┌──────────┬──────────────────────────────────────┐
│ sessions │  transcript (scrolls)                │
│ (260px)  │                                      │
│          │                                      │
│          ├──────────────────────────────────────┤
│          │  composer (bottom, not sticky-top)   │
└──────────┴──────────────────────────────────────┘
```

Composer moves to the **bottom**. Sticky-top composer fights a multi-turn
transcript after turn two.

### 5.2 Turn anatomy (production default)

A successful turn renders, in order:

1. **Question bubble** (navy) — always.
2. **Outcome line** — `N ردیف · ۱٫۲ ثانیه · گارد ✓` — always.
3. **SQL** — collapsed after first success this session; expanded on first turn and on failure.
4. **Result** — table, chart, scalar, zero-rows, or omitted-rows — always.
5. **Details drawer** (collapsed) — pipeline, assumptions editor, clarifications,
   LLM status strip, warnings, interpretation, feedback.

Pipeline visualization is **not** on every production turn. It lives in the
details drawer and in `?demo=1` / simulated mode only.

### 5.3 Admin

```
┌ sticky section nav ─────────────────────────────────┐
│  overview │ maintenance │ checks │ audit │ feedback │
│  cache │ config │ keys │ schema │ usage │ auth      │
└─────────────────────────────────────────────────────┘
  [ health summary rail — always first ]
  [ sections … ]
```

- Sticky jump nav (one line, horizontal scroll on narrow).
- **Health summary rail** at top: N failed checks · open feedback · maintenance on/off · last refresh.
- Per-section refresh replaced by **global auto-refresh (30s) + last-updated**; manual ↻ only on the rail.
- Severity stripes stay.

### 5.4 Site

- Same brand hues (teal/navy), not acid-green as primary identity.
- Optional night register for marketing **may** stay, but accent must be
  product teal, not a foreign neon.
- EN | FA as segmented control; other locales behind a menu.
- Hero SQL must be syntactically valid (every alias joined).
- Self-host fonts. Unminify CSS into `site/styles/` modules.

---

## 6. Interaction rules

| Rule | Rationale |
|---|---|
| One primary action per view | Ask. Issue key. Save. Not three equal buttons. |
| Destructive actions use status-critical, never brand | Revoke, clear cache, delete session |
| Focus-visible ring on every interactive element | `--focus-ring`; never `outline: none` without replacement |
| `prefers-reduced-motion` kills all animation | Already true — keep |
| Disabled sample stories only in simulated mode | Production does not advertise demo scripts |
| Copy always copies source of truth (Turn.sql), never highlighted DOM | Already true — keep |
| Keyboard: Enter submits, Shift+Enter newline, IME-safe | Already true in webapp — port to `web/` |

---

## 7. Icons

- **No emoji in UI chrome.** Render variance and contrast are not acceptable
  at this engineering level.
- Inline SVG, 16×16, 1.5px stroke, `currentColor`.
- Initial set: menu, close, sun/moon, brain/memory, refresh, copy, download,
  chevron, check, alert, database, shield.
- Lives in `web/assets/icons/` as a sprite or a small `icons.js` module
  (no build step — keep the static-file contract).

---

## 8. Accessibility bar

| Requirement | Standard |
|---|---|
| Body text contrast | ≥ 4.5:1 |
| Large / display | ≥ 3:1 |
| Status never colour-only | Stripe + word (already true on admin checks) |
| Dialogs | `role="dialog"`, `aria-modal`, focus trap, Esc closes |
| Live regions | Transcript `aria-live="polite"`; health `role="status"` |
| Tap targets | ≥ 44×44 on touch viewports |
| RTL | Logical properties only; identifiers isolated LTR |

---

## 9. Engineering constraints

1. **No build step** for `web/`. Static files, ES modules, vendored Prism.
2. **CSP:** `script-src 'self'` — no inline handlers. New icons ship as files or
   DOM-created nodes, not string HTML with handlers.
3. **Tokens in one file.** Surfaces import tokens; they do not redefine hex.
4. **Tests:** `tests/web_ui/` already asserts direction, XSS, structure.
   Every IA change lands with a corresponding test (markup contract, not
   pixel snapshots).
5. **Python surfaces** (if any remain) do not invent a second palette.
6. **Documentation:** this file + CHANGELOG + `web/README.md` update together.

---

## 10. Naming

| Kind | Convention | Example |
|---|---|---|
| CSS custom property | `--kebab-case` | `--status-good` |
| CSS class | `kebab-case`, block/element | `turn-outcome`, `admin-check` |
| JS module file | `kebab-case.js` | `render/turn.js` |
| JS function | `camelCase` | `createTurnCard` |
| JS constant | `SCREAMING_SNAKE` | `DEFAULT_BASE_URL` |
| Test file | `test_<area>_<behaviour>.py` | `test_web_ui_turn_anatomy.py` |
| Branch | `feature/`, `fix/`, `docs/`, `chore/` + short noun | `feature/ui-design-system` |
| Commit | Conventional Commits | `feat(web): collapse turn cards to outcome-first anatomy` |
| PR title | same as commit subject | |

---

## 11. Versioning and release

| Change | Version bump |
|---|---|
| Token extraction only, no IA change | minor (4.13.0) |
| Turn anatomy / topbar / admin nav — **breaking for muscle memory** | **major (5.0.0)** |
| Site rebrand + self-host fonts | minor, ships with 5.0 |
| `webapp/` removal | major, same 5.0 train |

Release checklist (each train):

1. Design + tests + docs land on `feature/…`
2. PR into `main`, human-week scope max
3. CI green, coverage gate holds (`web/` remains omitted from coverage)
4. CHANGELOG entry matches `core/version.py` (`tests/test_version.py`)
5. Tag `vX.Y.Z`
6. Close merged branches

---

## 12. Non-goals (5.0)

- New framework (React/Vue/Svelte). Out. Static ES modules stay.
- Pixel-perfect Figma parity process. Mockups are HTML in `docs/design/mockups/`.
- Full i18n of product chrome (product stays Persian-first; site keeps EN/FA+).
- Real-time collaboration, theming marketplace, plugin system.

---

## 13. Decision log

| # | Decision | Why |
|---|---|---|
| D1 | `web/` is the only product UI | Two product models cannot share one engine claim |
| D2 | Mode switch leaves chrome | Synthetic data next to live is an integrity hazard |
| D3 | Composer moves to bottom | Transcript is the product; the box is secondary after ask |
| D4 | Turn defaults to outcome-first | Five nested cards bury the answer |
| D5 | Pipeline not on every production turn | Tutorial asset ≠ analyst chrome |
| D6 | Admin gets sticky nav + health rail | Ten equal sections is not ops design |
| D7 | No emoji icons | Uncontrolled rendering; no a11y story |
| D8 | Self-host all fonts | Offline / privacy pitch is a lie otherwise |
| D9 | `webapp/` frozen | Auth is API work; not a second frontend |
| D10 | 5.0.0 major | Analyst muscle memory breaks; call it out |

---

## 14. Mockups

Interactive HTML (open in a browser, no server):

- [`mockups/analyst.html`](mockups/analyst.html) — production analyst shell + one turn
- [`mockups/admin.html`](mockups/admin.html) — admin with summary rail + jump nav
- [`mockups/tokens.html`](mockups/tokens.html) — palette, type, components

These are **approval artefacts**. Implementation starts only after they are
accepted or explicitly revised.

---

## 15. Next after approval

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for the sequenced TDD plan, branch
train, and 5.0.0 checklist.
