# Implementation Plan — UI 5.0

> Sequenced TDD plan. Starts only after `docs/design/DESIGN.md` mockups are approved.

## Current version

**4.12.0** (`core/version.py`)

## Target

**5.0.0** — major: analyst muscle memory changes (topbar, turn trust surface, admin nav).

---

## Train A — tokens + icons (can ship as 4.13.0 if needed alone)

| Step | Work | Tests |
|---|---|---|
| A1 | Extract `web/styles/tokens.css` from `style.css` | Markup: tokens file linked; no duplicate hex in style.css |
| A2 | `web/assets/icons/` SVG sprite + `icons.js` | No emoji in `index.html` / `admin/index.html` |
| A3 | Replace chrome emoji | Snapshot of class names on topbar controls |
| A4 | **SQL display:** `web/js/sql-display.js` (sql-formatter one-liners only + Prism T-SQL patch + editor palette) | `test_web_ui_sql_highlight.py` (prettify + highlight + copy exactness) |

Commits: `refactor(web): extract design tokens`, `feat(web): add icon set`, `refactor(web): replace chrome emoji with icons`, `feat(web): prettify and highlight generated SQL like a code editor`

## Train B — analyst IA (5.0.0 core)

| Step | Work | Tests |
|---|---|---|
| B1 | Topbar: health one-control + user menu | Topbar node list contract |
| B2 | Remove mode switch from DOM (keep `?live=0`) | Assert `#mode-switch` absent; `?live=0` still boots simulated — **extend** `test_web_ui_live_default.py`, do not delete |
| B3 | Composer position (bottom default; record pilot feedback) | Layout contract |
| B4 | Turn anatomy: **resolved + assumptions + clarifications visible**, slim stage strip, outcome line, collapsed SQL, result, details drawer | `tests/web_ui/test_web_ui_turn_anatomy.py` — chips **before** result node; stage strip in progressive mode |
| B5 | Sample stories only when simulated | Samples container hidden in live |

**Non-negotiable in B4:** api-contract §5/§7. Assumption chips and
`resolved_question` never live only in the drawer.

Commits: conventional, scope `web`.

## Train C — admin IA

| Step | Work | Tests |
|---|---|---|
| C1 | Health summary rail | Rail present with counts |
| C2 | Sticky section jump nav | All section ids linked |
| C3 | Global refresh + last-updated; demote per-section ↻ | |

## Train D — site alignment

| Step | Work |
|---|---|
| D1 | Unminify `site/styles.css` |
| D2 | Self-host fonts (drop Google CDN) |
| D3 | Brand hue alignment + fix hero SQL join |
| D4 | EN/FA segmented control |

## Train E — retire `webapp/` (**gated**)

**Gate (DESIGN.md §3.1):** do not start until API-key auth is confirmed as
the production path **or** FastAPI ships session login with tests.

| Step | Work |
|---|---|
| E0 | Confirm gate with operator evidence (who logs in today, how) |
| E1 | Document auth path via API keys / new login |
| E2 | Remove `webapp/` or move to `attic/webapp/` with README tombstone |
| E3 | Update root README and getting-started |

---

## Definition of done (5.0.0)

- [x] DESIGN.md tokens match CSS — §4.2 had published the pre-fix palette
      that fails 1.4.3; corrected, and now pinned by
      `tests/web_ui/test_web_ui_design_doc_matches_tokens.py`
- [x] No emoji in product chrome — the guard that claimed this stripped 📌
      before scanning for it; strip removed, verified by mutation
- [x] No mode switch in analyst topbar — `?live=0` verified still booting
      simulated, in the browser and by test
- [x] Assumptions + resolved question visible before result (contract §5/§7)
- [x] Slim stage strip during stream; summary after done
- [x] Turn anatomy tests green
- [x] Admin rail + nav tests green
- [x] Site: no Google Fonts, unminified, valid hero SQL
- [x] Dark + 375px verified
- [x] CHANGELOG 5.0.0 == `core/version.py`
- [x] Tag `v5.0.0`
- [x] PR merged; feature branches deleted

## PR policy

- Max human-week scope per PR.
- Merge when CI green and review criteria in DESIGN.md §9 hold.
- Branch names are plain product nouns.

## Version markers

| After | Version on `main` |
|---|---|
| Design commit (this branch only) | still **4.12.0** — docs are not a release |
| Train A merged | **4.13.0** |
| Trains B+C merged | **5.0.0** |
| Train D (if split) | fold into 5.0 or **5.0.1** polish |
| Train E | **5.1.0** unless gate was already true at 5.0 |
