# Integration and remediation plan — security branch × UI 5.0

> **Reads with** [`DESIGN.md`](DESIGN.md) (what the UI becomes),
> [`DESIGN-INVARIANTS.md`](DESIGN-INVARIANTS.md) (what it may not cost),
> [`IMPLEMENTATION.md`](IMPLEMENTATION.md) (MiMo's train plan, unchanged).

Two branches exist and neither knows about the other:

| Branch | Base | Carries |
|---|---|---|
| `security/audit-remediation` | `main @ 6ee96d7` + 8 commits | 19 audit fixes, 4 High. 2743 tests green, coverage 91.56% |
| `feature/ui-design-system` | `main @ 6ee96d7` + 8 commits | Design policy, tokens, icons, i18n, turn anatomy, admin IA. Train D in flight (uncommitted) |

They rewrite the same four files. A naive merge reopens a
privilege-escalation path. This plan sequences the work so that cannot happen.

---

## Order is the whole plan

```
1. rebase UI branch onto security branch      ← nothing else starts first
2. adopt the CSP the UI branch's own policy requires
3. fix the four measured layout defects
4. resume Train D
```

Step 1 before step 3 is not a preference. The layout fixes touch
`web/styles/style.css`, which the rebase also touches; doing them first
guarantees a second conflict over the same lines.

---

## Task 1 — rebase, do not merge

**Why rebase.** A merge asks someone to resolve four conflicted files once, at
speed, with no signal about which side matters. A rebase replays eight UI
commits one at a time onto a base that already has the security fixes, so each
conflict is small, local, and obviously about one feature.

```bash
git checkout feature/ui-design-system
git rebase security/audit-remediation
```

**Before starting:** `feature/ui-design-system`'s worktree has uncommitted
Train D work — `site/styles.css` (634 lines of unminification) and
`site/fonts/`. Commit it on the branch first. Do not stash: the stash stack is
shared across worktrees in this repository.

**Conflicts to expect, and how each resolves:**

| File | Resolution |
|---|---|
| `web/admin/main.js` | Keep the security branch's `escapeHtml` (5-character encode) and its `dataset`-based row construction. Re-apply the UI branch's *other* changes (health rail, jump nav, auto-refresh) on top. |
| `web/index.html`, `web/admin/index.html` | Keep the security branch's `<meta>` CSP. Take the UI branch's markup for everything else. |
| `api/admin_write_routes.py` | Keep the `principal_id` pattern. |
| `web/styles/style.css` | UI branch wins (it extracts `tokens.css`), but carry forward anything the security work added. |

**Verification, non-negotiable:**

```bash
PROJECT_CONFIG_DIR=project_config.example python -m pytest tests/security_audit tests/web_ui -q
```

`tests/security_audit/test_redesign_preserves_fixes.py` exists for exactly this
moment. It names the audit finding and says which side to keep. If it is red,
the rebase is not finished — do not proceed to Task 2.

---

## Task 2 — adopt the CSP the design policy already mandates

`DESIGN.md` §9.2 requires `script-src 'self'` with no inline handlers. The
security branch implements it; the UI branch does not. After the rebase it is
inherited for free.

Verified compatible already: the UI branch's pages contain **zero** inline
`on*=` handlers, and its one `innerHTML` (in `web/js/sql-display.js`) sets
`textContent` first and lets Prism escape. Nothing breaks.

One thing to preserve while reorganising: **`frame-ancestors` must not go back
into the `<meta>` policy.** A meta element cannot deliver it; the browser
ignores it and logs an error on every page load. The clickjacking rule lives in
`web/README.md` as an nginx block for the static server. A test asserts both
halves.

---

## Task 3 — the four measured layout defects

Spec: `tests/web_ui/test_web_ui_rtl_layout.py` — four failing tests, each with
the measurement that justifies it in the docstring. Do not edit that file; if a
test looks wrong, say so.

### 3.1 The conversation drawer never fully leaves the screen (worst of the four)

```css
[dir="rtl"] .sidebar { transform: translateX(-100%); }
```

Measured at 375px: a 300px panel starting at `left: 75` lands at `-225`,
leaving **75px on screen** covering the header and the Ask button.
`translateX(100%)` measures exactly 0 visible.

In RTL the drawer is anchored to the **right** edge, so it hides to the right.
`-100%` is the element's own width, not the distance to the far edge.

This single bug is why the mobile page looks like it has a dozen clipping
problems. It has one.

### 3.2 The API-key placeholder reorders

The field is `direction: ltr`, correctly — an API key is ASCII. Its placeholder
is Persian prose containing `API`, and the Latin run is what reorders it:

| | Read right-to-left |
|---|---|
| Today | `خود را وارد کنید API کلید` |
| Correct | `کلید API خود را وارد کنید` |

Either style `::placeholder` back to `rtl`, or start the field `rtl` and flip
to `ltr` with `:not(:placeholder-shown)` — the second also fixes alignment
while the analyst types. The value must stay LTR either way.

The admin panel's `کلید مدیریتی` is pure Persian and measured as **not**
reordering — only misaligned. Same fix covers it; different severity.

### 3.3 Tap targets

The interpretation toggle measures **81×20**. `DESIGN.md` §8 asks for 44×44 on
touch; the floor asserted is 24×24 (WCAG 2.5.8). The wrapping `<label>` carries
the padding — a 13px native checkbox is never the target on its own.

### 3.4 One breakpoint

`@media (max-width: 640px)` is the only viewport rule, so 768–1024 inherits the
desktop topbar the policy already calls a junk drawer. Add a tablet breakpoint
and verify at **375 / 768 / 1280**, `dir="rtl"`.

---

## Task 4 — resume Train D, then the open decisions

Train D (site unminify, self-hosted fonts, brand hues, hero SQL join) continues
as `IMPLEMENTATION.md` has it.

Three decisions in `DESIGN-INVARIANTS.md` need a human answer before 5.0 ships,
and none of them is a coding task:

1. **Default theme** (§4). All three mockups are light; the product renders
   dark. Adopting the mockups as drawn changes the default without anyone
   choosing it.
2. **Attribution** (§6). `IMPLEMENTATION.md` forbids AI trailers; the security
   branch's commits carry them. The two branches disagree and a merge inherits
   both.
3. **`webapp/` gate** (`DESIGN.md` §3.1). Still blocked on the same question
   the security audit reached independently: is the Flask login the production
   auth path at IME? Finding 13 and Train E are the same question asked from
   two directions.

---

## Definition of done for this plan

- [ ] `feature/ui-design-system` rebased onto `security/audit-remediation`
- [ ] `tests/security_audit/` fully green — 0 failures
- [ ] `tests/web_ui/test_web_ui_rtl_layout.py` fully green
- [ ] Full suite green; coverage gate (90%) holds
- [ ] `frame-ancestors` absent from every `<meta>` CSP
- [ ] No inline `on*=` handler in either page
- [ ] The three decisions above put to the repository owner, not guessed
