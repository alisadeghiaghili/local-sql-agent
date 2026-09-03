# Deployment runbook — first production week

Status: **operational checklist, not a design document**. Read this the day
you deploy, and again the day the pilot week ends. Every command assumes
you are in the repo root, on the machine that will actually run the server
(the same one whose `.env` / real environment variables the server reads).

Why this exists: this deployment's first week of real use produces
`logs/audit_log.jsonl` — the only source this project has ever had for real
accuracy and latency numbers. If the deployment stumbles, or the log is
lost, that week (and the users' confidence) does not come back. Everything
below is ordered so each step is verified before the next depends on it.

## 1. Issue an API key

Every route except `GET /health` requires a named API key
(`Authorization: Bearer <key>` — see `README.md`'s "Authentication" section
and `docs/api-contract-v2.md`). Issue one **per analyst**, not one shared
key for the web UI as a whole — the audit trail and the rate limiter both
key on principal id (`observability/audit.py`, `api/middleware.py`'s
`(principal, ip)` bucket), so one shared key behind one UI host makes every
analyst's traffic look like a single caller and collapses everyone into one
rate-limit bucket. `web/` (the static UI under `web/README.md`) is built
for this: each analyst enters their own key once, in their own browser, on
first use — the UI never ships or bakes in a key of its own.

```bash
python scripts/issue_api_key.py --id analyst-1 --name "Jane Analyst"
```

This prints the raw key **once** — hand it to that analyst directly (a
password manager share, or read aloud/typed by hand — never a group
channel or a shared doc) so they can paste it into the web UI's "کلید API"
field themselves; do not also collect it back into your own secrets
manager unless you specifically intend to be able to act as that analyst.
It also prints the `API_KEYS_JSON` array entry to paste into step 2.
If a key is lost, issue that analyst a new one; there is no way to recover
the old one from `key_sha256` alone (that is the point — see
`security/auth.py`'s module docstring).

Repeat for every analyst who will use the UI, plus one more for any other
real caller/integration, appending each entry to the same `API_KEYS_JSON`
array (`[{"id": ...}, {"id": ...}]`).

## 2. Set the environment

Copy `.env.example` to `.env` (if not already done) and fill in, at minimum:

- `DB_CONNECTION_URL` — the real warehouse connection string.
- `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` — the real LLM
  endpoint.
- `API_KEYS_JSON` — the array from step 1 (every issued key's entry).
- `PROJECT_CONFIG_DIR` — leave unset (defaults to `project_config/`, this
  deployment's real domain data) unless you deliberately mean to run
  against the sample `project_config.example/` template.

Leave `RATE_LIMIT_*`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `AUTH_REQUIRED`,
and `MAX_CONCURRENT_REQUESTS` at their shipped defaults unless step 3 below
tells you otherwise for your specific expected concurrency — see
`config.Settings.rate_limit_requests` / `.log_backup_count` for the
reasoning behind each default before changing it.

## 3. Run the preflight

```bash
python scripts/verify_deployment.py
```

This must print `0 failed` before you go further. Every line is
`[PASS] / [FAIL] / [SKIP]` with a reason — a `[FAIL]` tells you exactly
what to fix (a placeholder still in `.env`, an unreachable database, a
model name the endpoint doesn't actually serve, no API key configured, an
unwritable log directory, a `project_config/` that fails to load, or a
rate limit too tight for your expected number of analysts). `[SKIP]` is
not a failure — it means a check couldn't run (e.g. no database reachable
yet to test the row cap against), not that something is wrong.

Two optional environment variables sharpen two of the checks without
changing anything persistent:

- `VERIFY_API_KEY=<raw key from step 1>` — proves that *specific* key
  round-trips through real authentication, not just that some key is
  configured.
- `VERIFY_EXPECTED_ANALYSTS=<N>` — states how many concurrent analysts you
  actually expect behind the smallest configured key's bucket, so the
  rate-limit check reasons about your real deployment shape instead of
  the default assumption of 10.

```bash
VERIFY_API_KEY=<raw key> VERIFY_EXPECTED_ANALYSTS=15 python scripts/verify_deployment.py
```

Do not proceed to step 4 with any `[FAIL]` outstanding.

## 4. Start the server

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

(Add `--workers N` for more than one process if your expected load needs
it — the audit log, rate limiter, and query cache are all per-process
in-memory state today, so multiple workers each keep their own rate-limit
buckets and cache; this does not affect correctness, only means each
worker's rate limit applies independently.)

## 5. Confirm the startup banner

The very first thing logged, before any config is even validated, is the
provenance banner (`core/provenance.log_startup_notice`) — confirm it
appears in the server's log output:

```
Auction NLQ Engine — <version/licence line identifying this codebase>
```

If startup instead exits immediately with `RuntimeError: ...`, the
preflight in step 3 should have already caught the same problem — go back
and re-run it. The two most common fail-closed exits, both intentional:

- `Invalid configuration: ...` — a `Settings.validate()` failure (a
  placeholder left in `.env`).
- `AUTH_REQUIRED is true but API_KEYS_JSON has no configured keys` —
  step 1/2 was skipped or the entry didn't make it into `.env`.

Also confirm `System prompt loaded (N chars)` appears — a missing
`prompts/system_prompt.md` is a packaging error, not a config one.

## 6. Confirm the audit log is being written

Send one real (or throwaway) authenticated query, then confirm a line
landed:

```bash
curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer <a real issued key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "test", "mode": "sql"}'

tail -n 1 logs/audit_log.jsonl
```

The line should be a JSON object with today's `timestamp`, the
`request_id` your response's `X-Request-ID` header also carries, and a
`guard`/`llm` block. If `logs/audit_log.jsonl` doesn't exist or didn't
grow, re-run `python scripts/verify_deployment.py` — the audit-log
writability check will say exactly why (directory not writable, wrong
`LOG_DIR`, ...). Remember: a broken audit write never fails the user's
query (`observability/audit.py`'s second hard rule) — it fails *silently*
from the caller's point of view, which is exactly why this step exists.

## 7. During the week

Nothing to do by default — `logs/audit_log.jsonl` rotates on its own
(`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`, defaults sized so a single
organisation's first weeks cannot plausibly exhaust the retained history;
see `config.Settings.log_backup_count`). As a belt-and-suspenders measure
for a week whose data cannot be re-collected, consider copying
`logs/audit_log.jsonl*` to a second location once a day (a scheduled
`cp`/backup job, or manually) — the rotation defaults are generous, but a
second copy costs little and removes any dependency on this specific
disk surviving the week.

## 8. At week's end: run the analyser

```bash
python scripts/analyze_audit_log.py > report.txt
```

With no arguments this reads `logs/audit_log.jsonl` plus any rotated
`.1`, `.2`, ... backups automatically. The output is a **fully aggregated**
report — record counts, latency percentiles, `finish_reason`/error-code
distributions, cache hit rates, SQL-shape clusters, correction-round
stats — with no question text, no generated SQL, and no error messages
anywhere in it. `report.txt`'s own `mode` line will read `aggregate_safe`.
**This is the file to send back.**

Before sending it, sanity-check the top of the report:

- `records_by_model` — confirms the week's traffic is real (not
  `mock:stub`/`ollama:test` left over from local development).
- `finish_reason distribution` — any non-trivial `length` count means
  `llm_num_predict` was too low for at least some real questions and
  should be raised.
- `cache_behaviour`'s `prefix_cache_hit_rate` — the first real measurement
  of whether Phase 2's static-prefix latency premise actually held under
  real traffic.

If, and only if, you have separately decided sharing example questions is
acceptable (e.g. you are debugging a specific miss with whoever wrote this
code), re-run with `--include-examples` — this adds a small number of
verbatim questions and is clearly labelled
`mode: aggregate_with_examples` with a warning banner in the text output.
Never send an `aggregate_with_examples` report anywhere by default; treat
it the same way you would treat the raw log itself.

```bash
python scripts/analyze_audit_log.py --include-examples > report_internal.txt
```

Send `report.txt` (or `report.json` via `--json`, for anyone who wants to
compute their own numbers off the aggregates). Do not send
`logs/audit_log.jsonl` itself, or `report_internal.txt`, unless that
decision has been made explicitly and separately.
