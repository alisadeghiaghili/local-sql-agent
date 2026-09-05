# Local SQL Agent — conversational web UI

A self-contained, no-build browser UI for the multi-turn "session" workflow
described in `docs/api-contract-v2.md`: ask a question in Persian, watch the
five-stage pipeline, read the generated T-SQL, see the result table, and ask
follow-up questions that refine the previous turn ("از بین آن‌ها …" / "among
those …").

It never pretends a real query ran when it did not — see **Modes** below.

## Quick start

This is a **static client**. It is served as files; it does not import
any Python from this project, and it never runs in the same process as
the backend.

That means **two terminals, in two different directories**. Getting this
wrong produces `ModuleNotFoundError: No module named 'api'` — which is
the backend being started from inside `web/`, where the `api` package
does not exist. There is no `api` package on PyPI to install; the one it
wants is this repository's own, and it is only importable from the repo
root.

**Terminal 1 — the backend, from the repository root:**

```powershell
cd <repo-root>          # NOT web/
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

**Terminal 2 — the static files, from this folder:**

```powershell
cd <repo-root>\web
python -m http.server 8080
# then open http://localhost:8080
```

`python -m http.server` only hands files to the browser. **زندهٔ API**
(live) is this page's default mode (see **Modes** below) — if the backend
on port 8000 is not actually running yet, the health pills go red with an
honest "backend unreachable, run uvicorn or switch to simulated" message
rather than silently falling back to demo data. Switch to نمایشی
(simulated) with the topbar toggle, or load with `?live=0`, if you just
want the demo.

Click one of the six "داستان نمونه" (sample story) buttons, or type your own
question and press **پرسش** (Ask). Free-typed questions are matched
best-effort against the same six scripted turns in `js/data.js` (see
`SCENARIO_MATCH_HINTS` in that file); anything else shows an honest "not in
the demo script" notice rather than guessing.

## Modes

### زندهٔ API — Live (default)

Talks to the real FastAPI backend using `fetch` against the endpoints in
`docs/api-contract-v2.md` §3 (`/v2/sessions`, `/v2/sessions/{id}/turns`,
`?stream=1` SSE, `PATCH .../assumptions`). This is the default because a
deployment is live and the analysts opening it expect real answers —
defaulting to simulated would mean synthetic, made-up numbers render in
the exact same UI as real ones (see `web/js/state.js`'s comment on
`state.mode`).

The backend base URL is **not** a top-bar control — it is deployment
configuration, not something an analyst should see or set (a wrong value
there looks exactly like a dead backend). A deployment sets it once in
`web/js/config.js`'s `DEFAULT_BASE_URL`; an operator can still override it
per-load with `?base=http://host:port` for debugging, or persist an
override to one browser's `localStorage` (`web/js/state.js`). Switch modes
with the **نمایشی** / **زندهٔ API** buttons in the top bar, `?live=1`, or
`?live=0` to force simulated (e.g. `index.html?live=0`, or
`index.html?base=http://localhost:8000` to point a live load somewhere
else for one load).

No live Ollama or SQL Server handy? `scripts/dev_v2_demo_server.py` runs
the real FastAPI app end to end against a synthetic in-memory SQLite
database and a keyword-dispatching stub model — see that file's docstring.
It is manual-verification tooling only, not a substitute for testing
against a real model/database.

### نمایشی — Simulated

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

### Authentication (Phase 8)

Every route the real backend serves requires `Authorization: Bearer <key>`
except `GET /health` (`docs/api-contract-v2.md` §11). This UI does **not**
ship a key — it is a static file served straight to the browser, and
anything embedded in it at serve time is readable by anyone who opens dev
tools. Instead:

- The "کلید API" field is in the top bar whenever **زندهٔ API** mode is
  active — which, since live is now the default, means from the first
  load. Ask a question (or open the memory panel, or start a new
  conversation) before entering one and the UI prompts you to fill it in
  first. Get your key from whoever administers this deployment — they
  issue it with `scripts/issue_api_key.py`, one per analyst.
- The key is saved in this browser's `localStorage` (via `js/apikey.js`)
  and sent as `Authorization: Bearer <key>` on every authenticated call.
  It is never logged, never put in a URL, and never echoed back in an
  error message. `GET /health` works with no key at all; if one happens
  to be stored it is sent anyway, which is what unlocks the `model` field
  in the health pill's tooltip.
- **Every analyst needs their own key**, not one shared key pasted into
  everyone's browser. Sharing a key defeats the point of Phase 8's
  per-caller identity: `observability/audit.py`'s audit trail attributes
  by `principal_id`, and `api/middleware.py`'s rate limiter buckets on
  `(principal, ip)` — one shared key collapses both back into "the whole
  office looks like one caller", which is exactly the shape a shared
  service key had before this UI existed.
- A `401` (missing or rejected key) clears the stored key and re-prompts
  with "کلید API رد شد یا نامعتبر است" instead of a generic error. A
  `429` is shown as a rate-limit notice, not a query/model failure — the
  server's error body says so explicitly and this UI passes that through.
- Change or clear your key any time from the same "کلید API" field in the
  top bar (visible whenever **زندهٔ API** mode is active).

**As of Phase 3, the backend implements `/v2/*`** (`api/v2_routes.py`,
mounted onto `api/server.py`) — sessions, turns (including `?stream=1`
SSE), and `PATCH .../assumptions` are real endpoints backed by
`session.engine.TurnEngine`. Live mode was written against the frozen
contract before that backend existed, so no frontend changes were needed
to talk to it; it still degrades honestly for anything that genuinely
isn't there:

- `GET /health` is called to populate the three status pills (API / LLM /
  DB). `web/js/api.js` reads the *real* field names from
  `api/models.py::HealthResponse` — `status`, `openai`, `database`, `model`.
  This field name has drifted (and been silently wrong here) twice: the
  original reference demo read `h.openai` when the backend field was
  actually `ollama`; a later fix switched this file to `h.ollama`; the
  backend was then refactored to a single OpenAI-compatible provider and
  the field went back to `openai`, which this file did not track until
  the web-UI-auth pass caught it. If the LLM pill ever looks permanently
  wrong again in live mode, check this field name against
  `api/models.py::HealthResponse` first.
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
├── assets/
│   ├── fonts/               # Vazirmatn-Regular.woff2, Vazirmatn-Bold.woff2
│   └── vendor/              # Prism (core + SQL language + theme) — see "SQL highlighting"
├── js/
│   ├── main.js              # bootstrap, ask flow, simulated/live dispatch
│   ├── state.js             # app state + localStorage (theme, base URL, mode)
│   ├── config.js             # THE ONE FILE A DEPLOYMENT EDITS — default backend base URL
│   ├── api.js                # live-mode transport (contract §3, §6, §7, §11)
│   ├── apikey.js              # per-analyst API key storage (contract §11)
│   ├── data.js                # SCENARIO: the six synthetic sample turns
│   └── render/
│       ├── turn.js            # composes one full turn card; SQL syntax highlighting
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

## SQL highlighting

The generated-SQL block is syntax-highlighted with
[Prism](https://prismjs.com/), vendored locally (core + the SQL language
component) under `web/assets/vendor/prism.min.js` /
`prism-sql.min.js` — no CDN, same offline requirement as the font above.
`web/assets/vendor/prism-sql-theme.css` maps Prism's token classes to this
app's own existing colour tokens (`--teal`, `--amber`, `--muted`, etc. —
see `styles/style.css`), so it already adapts to both the light and dark
theme rather than shipping Prism's own hardcoded theme colours.

Highlighting is presentation only (`web/js/render/turn.js`'s
`highlightSql`): it always sets the code element's plain text first, and
only then overlays Prism's markup, falling back silently to the plain
text if Prism ever fails to load or throws. The "کپی" (copy) button never
reads from that markup — it always copies the exact original SQL string
from the Turn object.

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
- No external runtime dependency beyond the bundled fonts and the
  vendored Prism (SQL syntax highlighting) — no CDN, ever.
- No backend base-URL control in the top bar — that is deployment
  configuration (`web/js/config.js`), not an analyst-facing setting.
- Simulated mode never claims to have executed a real query; the footer and
  a persistent notice always say which mode is active.
- Live mode never synthesizes a Turn to hide a missing or failing backend
  endpoint — every failure path (404, network error, transport error, 401,
  429) shows an explicit notice.
- No API key is ever embedded in the served files — each analyst enters
  their own, kept only in their own browser's `localStorage`. See
  "Authentication" above.
