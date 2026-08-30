# Local SQL Agent — conversational web UI

A self-contained, no-build browser UI for the multi-turn "session" workflow
described in `docs/api-contract-v2.md`: ask a question in Persian, watch the
five-stage pipeline, read the generated T-SQL, see the result table, and ask
follow-up questions that refine the previous turn ("از بین آن‌ها …" / "among
those …").

It never pretends a real query ran when it did not — see **Modes** below.

## Quick start

Serve this folder with any static file server (no build step, no npm
install, no CDN dependency):

```powershell
cd web
python -m http.server 8080
# then open http://localhost:8080
```

Click one of the six "داستان نمونه" (sample story) buttons, or type your own
question and press **پرسش** (Ask). Free-typed questions are matched
best-effort against the same six scripted turns in `js/data.js` (see
`SCENARIO_MATCH_HINTS` in that file); anything else shows an honest "not in
the demo script" notice rather than guessing.

## Modes

### نمایشی — Simulated (default)

Replays the fixed six-turn conversation in `js/data.js` through the five
pipeline stages with realistic timing. All data — customer names, contract
values, row counts — is synthetic and generated with a seeded PRNG
(`mulberry32`), so the same names and numbers appear on every reload. Nothing
here is real Auction/Contract/Customer data.

The six scripted turns exercise every UI state called for by the contract:

| Turn | What it demonstrates |
|---|---|
| `t_01` | fresh, unambiguous, 342 matched / 100 shown (`truncated: true`) |
| `t_02` | refines `t_01`; inherited session context; the §2 **policy** scope assumption (locked, non-editable); a `refinement_scan_cap` warning |
| `t_03` | same intent, asked without the session cue → `is_ambiguous: true`, `default`-sourced assumptions, one-click clarification offers |
| `t_04` | a destructive statement, blocked by the guard (`verdict: "rejected"`) |
| `t_05` | total LLM transport failure (`attempts: 3`, `endpoint_status: 0`) — the card still renders everything it has |
| `t_06` | retry of `t_05`'s question, succeeds, with one self-correction round |

### زندهٔ API — Live

Talks to the real FastAPI backend using `fetch` against the endpoints in
`docs/api-contract-v2.md` §3 (`/v2/sessions`, `/v2/sessions/{id}/turns`,
`?stream=1` SSE, `PATCH .../assumptions`). Enable it with the **زندهٔ API**
button and enter the backend base URL (defaults to `http://localhost:8000`),
or start directly with `index.html?live=1&base=http://localhost:8000`.

No live Ollama or SQL Server handy? `scripts/dev_v2_demo_server.py` runs
the real FastAPI app end to end against a synthetic in-memory SQLite
database and a keyword-dispatching stub model — see that file's docstring.
It is manual-verification tooling only, not a substitute for testing
against a real model/database.

**As of Phase 3, the backend implements `/v2/*`** (`api/v2_routes.py`,
mounted onto `api/server.py`) — sessions, turns (including `?stream=1`
SSE), and `PATCH .../assumptions` are real endpoints backed by
`session.engine.TurnEngine`. Live mode was written against the frozen
contract before that backend existed, so no frontend changes were needed
to talk to it; it still degrades honestly for anything that genuinely
isn't there:

- `GET /health` is called to populate the three status pills (API / LLM /
  DB). `web/js/api.js` reads the *real* field names from
  `api/models.py::HealthResponse` — `status`, `ollama`, `database`, `model`.
  (The single-shot reference demo this UI was styled after had a real bug
  here: it read `h.openai`, a field that does not exist on the response, so
  the LLM pill was permanently wrong in live mode. That bug is not repeated
  here.)
- Asking a question calls `POST /v2/sessions`. Against an OLDER backend
  build without v2 support this would return `404`, which `web/js/api.js`
  promotes to a `V2NotSupportedError`; the UI shows a clear warning —
  "بک‌اند نسخهٔ گفتگویی v2 را هنوز پشتیبانی نمی‌کند" — and automatically
  falls back to simulated mode. (This message hardcodes the phrase "404
  روی /v2/sessions" even when a *different* v2 call is what actually
  404'd, e.g. `PATCH .../assumptions` against an unknown `turn_id` — a
  pre-existing minor imprecision in this fallback text, not something
  Phase 3 changed.) It never fabricates a Turn to paper over a missing
  endpoint; any transport or HTTP error is shown in a notice, not silently
  swallowed.
- On any other network failure (backend not running at all), the health
  pills go red with a hint to start `uvicorn api.server:app`.

**CORS:** `api/server.py` now registers `CORSMiddleware`, controlled by
the `CORS_ALLOWED_ORIGINS` setting (comma-separated origins; empty by
default, which blocks every cross-origin request). Serving this folder
from a different origin/port than the API (the normal case when following
"Quick start" above) requires the operator to set
`CORS_ALLOWED_ORIGINS=http://localhost:8080` (or whatever origin/port this
folder is served from) before starting the API — same-origin deployments
never need this at all. See `docs/api-contract-v2.md` §9 and `config.py`
for the full setting.

## Structure

```
web/
├── index.html              # shell: topbar, composer, transcript container
├── styles/
│   ├── style.css           # all styling — RTL-first, logical properties,
│   │                       #   light/dark tokens, responsive rules
│   └── fonts.css           # @font-face for the bundled Vazirmatn woff2s
├── assets/fonts/            # Vazirmatn-Regular.woff2, Vazirmatn-Bold.woff2
├── js/
│   ├── main.js              # bootstrap, ask flow, simulated/live dispatch
│   ├── state.js             # app state + localStorage (theme, base URL)
│   ├── api.js                # live-mode transport (contract §3, §6, §7)
│   ├── data.js                # SCENARIO: the six synthetic sample turns
│   └── render/
│       ├── turn.js            # composes one full turn card
│       ├── assumptions.js     # basis indicator, assumption chips, clarifications
│       ├── pipeline.js        # the 5-step pipeline list
│       ├── table.js           # result table + warnings
│       └── llm-status.js      # the §6 LLM status strip
└── README.md
```

## Font

Vazirmatn is vendored locally as two static `woff2` weights (Regular 400,
Bold 700) under `web/assets/fonts/` — no Google Fonts or other CDN, so the
UI works fully offline. If the font files are ever missing (e.g. this
directory was copied without `web/assets/fonts/`), `styles/fonts.css` falls
through to `"Segoe UI", Tahoma` which render Persian adequately on Windows
but without Vazirmatn's Persian-specific metrics.

## Adding a scenario

Scenarios live entirely in `js/data.js` as `Turn` objects matching
`docs/api-contract-v2.md` §4 — there is no schema validation at runtime, so
match the shape of an existing turn (e.g. `t_01`) closely. To add a new
sample turn:

1. Add a `Turn` object to `SCENARIO.turns` with a unique `turn_id`. If it
   refines an earlier turn, set `basis.kind = "refines"` and
   `basis.refines_turn_id` to that turn's id.
2. Generate any rows deterministically — seed a `mulberry32(...)` PRNG with a
   fixed integer, the same pattern every existing turn uses, so the demo
   stays stable across reloads. Never hand-write "real-looking" data that
   could be mistaken for actual customer/contract records.
3. Populate `llm` via the `llm({...})` helper already in the file — it
   derives `total_ms`, `tokens_per_second`, and `prefix_cache_hit` from the
   fields you pass, per contract §6.
4. Add an entry to `SCENARIO_MATCH_HINTS[turn_id]` — an array of Persian
   substrings used to best-effort-match a free-typed question back to your
   scripted turn (see `matchScriptedTurn` in `main.js`).
5. The sample button and pipeline animation are wired automatically from
   `SCENARIO.turns` — no other file needs to change.

## What this UI does not do

- No build step, bundler, or package.json — open the files as-is.
- No external runtime dependency beyond the bundled fonts.
- Simulated mode never claims to have executed a real query; the footer and
  a persistent notice always say which mode is active.
- Live mode never synthesizes a Turn to hide a missing or failing backend
  endpoint — every failure path (404, network error, transport error) shows
  an explicit notice.
