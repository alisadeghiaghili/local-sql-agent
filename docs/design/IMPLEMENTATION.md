# Implementation Plan — UI 5.0

> Sequenced TDD plan. Starts only after `docs/design/DESIGN.md` mockups are approved.

## Current version

**4.12.0** (`core/version.py`)

## Target

**5.0.0** — major: analyst muscle memory changes (topbar, composer position, turn anatomy).

---

## Train A — tokens + icons (can ship as 4.13.0 if needed alone)

| Step | Work | Tests |
|---|---|---|
| A1 | Extract `web/styles/tokens.css` from `style.css` | Markup: tokens file linked; no duplicate hex in style.css |
| A2 | `web/assets/icons/` SVG sprite + `icons.js` | No emoji in `index.html` / `admin/index.html` |
| A3 | Replace chrome emoji | Snapshot of class names on topbar controls |

Commits: `refactor(web): extract design tokens`, `feat(web): add icon set`, `refactor(web): replace chrome emoji with icons`

## Train B — analyst IA (5.0.0 core)

| Step | Work | Tests |
|---|---|---|
| B1 | Topbar: health one-control + user menu | Topbar node list contract |
| B2 | Remove mode switch from DOM (keep `?live=0`) | Assert `#mode-switch` absent; `?live=0` still boots simulated |
| B3 | Composer to bottom | Layout contract: composer after transcript in DOM order for a11y |
| B4 | Turn anatomy: outcome line + collapsed SQL + details drawer | `tests/web_ui/test_web_ui_turn_anatomy.py` |
| B5 | Sample stories only when simulated | Samples container hidden in live |

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

## Train E — retire `webapp/`

| Step | Work |
|---|---|
| E1 | Document auth path via API keys only |
| E2 | Remove `webapp/` or move to `attic/webapp/` with README tombstone |
| E3 | Update root README and getting-started |

---

## Definition of done (5.0.0)

- [ ] DESIGN.md tokens match CSS
- [ ] No emoji in product chrome
- [ ] No mode switch in analyst topbar
- [ ] Composer at bottom
- [ ] Turn anatomy tests green
- [ ] Admin rail + nav tests green
- [ ] Site: no Google Fonts, unminified, valid hero SQL
- [ ] CHANGELOG 5.0.0 == `core/version.py`
- [ ] Tag `v5.0.0`
- [ ] PR merged; feature branches deleted

## PR policy

- Max human-week scope per PR.
- Merge when CI green and review criteria in DESIGN.md §9 hold.
- No co-author trailers. No AI attribution. Branch names are plain product nouns.
