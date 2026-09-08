# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [4.11.2] — 2026-09-08

### Fixed

- **The admin panel laid out its English output right-to-left.** The page
  is `dir="rtl"`, which is right for its Persian chrome and wrong for what
  it displays: deployment-check cards carry
  `scripts/verify_deployment.py`'s own text — exceptions, SQL, connection
  URLs, Windows paths. Rendered RTL, `Settings.validate()` displayed as
  `()Settings.validate`, and a connection URL broke across lines in the
  wrong order. This is output an operator reads character by character to
  find a typo in. The check card is now LTR as a block, since its name,
  status pill and detail are one English sentence.

- **The keys table showed `.env` as `env.`** Its leading dot is a neutral
  character, so an RTL paragraph moved it to the end — a filename
  displayed wrong, in the column that says where a key came from. Cells in
  that table, and values in the key/value lists, now take direction from
  their own content (`unicode-bidi: plaintext`), because the same field
  legitimately holds a Persian maintenance note on one row and a path on
  the next.

- **Principal ids and role names are isolated**, so a hyphen inside
  `ops-1` cannot be reordered by the surrounding paragraph — the treatment
  `style.css` already gives numbers via `.num`.

### Added

- `tests/web_ui/test_web_ui_admin_direction.py`. The `.env` case is why
  these are assertions rather than a review habit: four characters, looks
  almost right, and invisible in the markup. It was found by rendering the
  panel and looking at it.

### Checked, and already correct

The rest of the panel was audited against the same failures found in the
analyst UI — page-level overflow, content clipped outside a scrollable
ancestor, labels rendered outside their container — at 1280px, 900px and
760px. None of them are present: the panel has no SVG, so the chart's
anchor bug does not apply, and the topbar inherits the wrapping fix from
4.11.1's shared `style.css`.

---

## [4.11.1] — 2026-09-08

### Fixed

- **Chart labels rendered outside the chart.** `web/js/render/chart.js`
  computes every position left-to-right — x grows rightward, and
  `edgeSafeLabel` flips `text-anchor` between `"start"` and `"end"` to keep
  a label inside `[0, W]`. But `text-anchor` resolves against the inline
  base direction, and both host pages are `dir="rtl"`, so the SVG
  inherited rtl and every one of those decisions meant its opposite.

  Measured in a browser: a label clamped to `x=2` with anchor `"start"`
  rendered at `x=-140`, 140 units outside the box — on the exact side the
  clamp existed to protect. Every label the function tried to rescue was
  precisely the one it threw out. The SVG now pins `direction: ltr`;
  Persian text inside is unaffected, since base direction sets where a run
  is anchored, not how it is shaped.

- **A mispositioned label escaped the chart entirely.** `.chart-block svg`
  carried `overflow: visible`, so a label outside the viewBox drew over
  the card around it rather than being clipped. Now `hidden`: a future
  geometry mistake reads as a cut-off label inside the chart instead of
  text across the layout.

- **Summed rial figures overflowed their summary tile.** The strip's grid
  tracks were sized at 140px for counts and percentages; a twenty-digit
  total at 17px needs far more, and a grid item's default `min-width: auto`
  meant the `1fr` track could not shrink to contain it. Tracks widened,
  `min-width: 0` so the track wins, and the value now scales down and
  wraps before it would clip — the digits cut in RTL are the most
  significant ones.

- **Ranking labels truncated to about twelve characters**, which turns
  most real company names into an ellipsis and a hint. The category track
  goes from 130px to 176px; the bars lose the difference and stay legible,
  where an unreadable label makes the whole ranking unreadable.

### Added

- `tests/web_ui/test_web_ui_chart_direction.py`. The existing chart test
  drives the real module against a stub DOM with no layout, fonts or bidi
  algorithm, so this class of bug was structurally invisible to it. The
  new test asserts the declaration whose absence *was* the defect, and
  checks its own premise: if `chart.js` ever resolves anchors against the
  document direction, the test says to delete itself along with the CSS
  rather than work around it.

---

## [4.11.0] — 2026-09-08

### Fixed

- **The web UI's five pipeline steps now actually tick.** They never did.
  The SSE endpoint emitted exactly one `stage` event, before the engine
  was even called, naming a stage `"plan"` — an id no step has. So
  `renderPipeline`'s `setStage` looked for `[data-step="plan"]`, found
  nothing, and returned without a word; all five steps sat at "در انتظار"
  for the whole turn while the answer appeared beside them.

  Nothing in the stack was broken enough to notice: the event was
  well-formed, the stream was valid, the turn was correct, and the one
  test covering the stream asserted the single frame as if it were the
  contract.

  Progress is now live. `StageTimer` already knew when each stage started
  and finished and had no way to say so; it takes an optional observer,
  `TurnEngine.ask` threads one through, and the endpoint bridges the
  worker thread back to the event stream with a queue. Each of the five
  steps reports `running` when it starts and `done` — or `error` — when it
  ends, including the repeats a correction round genuinely produces.

- **Generated SQL is formatted before it is displayed.** `sql_display` was
  the model's own text, so its layout was the model's mood: a tidy
  multi-line statement for one question and a single 300-character line
  for the next, which reads as the system formatting sometimes and not
  others. It is now re-rendered with sqlglot — already a dependency, and
  already the thing that parsed this SQL to validate it.

  Display only. `Turn.sql` stays byte-for-byte what the guard validated
  and the database ran; re-rendering that would mean the audit trail
  recorded a statement nobody executed. `pretty_sql` never raises — SQL it
  cannot re-render is shown as generated, because no formatting problem is
  worth failing a successful query over.

---

## [4.10.3] — 2026-09-08

### Added

- **Three badges for guarantees a build step actually enforces**, each
  linking to the step that makes it true: `SQL guard: AST allowlist`
  (`security/sql_guard.py`, parsing with sqlglot rather than matching
  strings), `engine: no domain literals`
  (`tests/test_no_domain_literals.py`, which fails the build if a real
  warehouse name reappears in engine source), and
  `doctests: enforced in CI` (a required workflow step across fourteen
  packages, not an optional one).

  `tests/test_readme_claims.py` covers these the way it already covers the
  coverage figure, but against a different failure: not drift, but the
  backing quietly going away. Each badge must still be present, still link
  to its evidence, and that evidence must still exist and still contain
  the thing being claimed — so a guard deleted in a refactor takes the
  badge down with it instead of leaving the README promising something
  that stopped being true.

---

## [4.10.2] — 2026-09-08

### Changed

- **Coverage gate raised 85% → 90%.** The README now advertises the
  measured figure, and a badge saying 92% with a gate at 85 is a number
  nobody is holding: coverage could fall seven points with every build
  still green and the README still claiming otherwise. Measured coverage
  is 92.84%, so 90 leaves real headroom without letting the claim rot.

- **README badges rebuilt around what can be verified.** The CI status
  and the release version now read GitHub directly and cannot go stale.
  Coverage, test count, Python versions and licence remain fixed
  strings — every one of them re-measured for this release, because two
  were wrong: `Tests-2573` never matched any real count, and
  `Version-4.6.1` was three releases behind.

### Added

- **`tests/test_readme_claims.py` watches the coverage badge.** The real
  percentage cannot be measured from inside the run being measured, but
  the relationship between the advertised figure and the gate enforcing
  it is a pure configuration fact, and that gap is the failure mode. The
  build now fails if the badge promises more than `fail_under` holds up,
  if the gate moves up without the badge following, or if a hardcoded
  version badge comes back. Same idea as `tests/test_version.py`, applied
  to the claim whose truth lives in another file.

### Documentation

- **The README described a project three releases old.** Its feature table
  had no admin panel and no analyst web UI — the two largest things
  shipped since it was written — and stated the test count as 2,235 in
  three separate places. The configuration reference, corrected in 4.10.1,
  is now joined by a Tests section that says what the coverage number does
  *not* cover: the interactive wizards excluded by policy, and the two
  modules excluded as a declared ratchet.

---

## [4.10.1] — 2026-09-08

Documentation catch-up for 4.9.0 and 4.10.0, which shipped code without
updating the prose that described it.

### Fixed

- **`web/README.md` said a 401 clears the stored key.** It has not since
  4.10.0 — the opposite is now true, and deliberately so. This was a
  documented promise the code no longer kept.

### Documentation

- **`docs/fa/getting-started.md` §2.9 — "مدل ریزنینگ و «جوابی نگرفتم»".**
  The whole failure, end to end: why a reasoning model returns nothing at
  the default token cap, the four audit fields that identify it *together*
  (`finish_reason: length`, `completion_tokens` exactly at the cap,
  `reasoning_detected: true`, empty SQL), and the two fixes with a
  per-server table for `LLM_EXTRA_BODY`. Including the warning that
  matters most: a server silently ignores a field it does not recognise,
  so the effect has to be confirmed in the audit record rather than
  assumed. This is the guide the deployment that hit this actually
  follows, and it said nothing about any of it.

- The Persian troubleshooting table gains rows for `LLM_OUTPUT_TRUNCATED`
  and for the pre-4.10.0 form of the same failure, and its 401 row now
  says the stored key is no longer cleared.

- §2.2 notes that the key field collapses once a key is saved — a reader
  looking for the field after saving is not signed out.

- `README.md`'s configuration reference gains `LLM_NUM_PREDICT` and
  `LLM_EXTRA_BODY`, neither of which was listed, and its `API_KEYS_JSON`
  row now shows the three capability flags 4.7.0 made issuable.

- `docs/en/tutorial.md`'s error table gains `EmptySQLResponseError` and
  `TruncatedSQLResponseError`; `docs/api-contract-v2.md` says what
  `finish_reason: "length"` with empty SQL now surfaces as; and
  `docs/deployment-runbook.md`'s "raise `llm_num_predict`" advice now
  names the cheaper alternative for a reasoning model.

---

## [4.10.0] — 2026-09-08

### Fixed

- **A completion cut off at the token cap is named as such, and not
  retried.** When a reasoning model spends its whole budget thinking, the
  response is empty with `finish_reason="length"`. The correction loop
  treated that as bad SQL and re-prompted twice — "the SQL query you
  generated failed / --- FAILED SQL --- (nothing) / --- ERROR --- LLM
  returned an empty response" — which cannot succeed, because nothing
  about the truncation depends on the question. Three rounds, ~17 seconds,
  one certain outcome.

  It now returns on the first round with `LLM_OUTPUT_TRUNCATED` and a
  message naming the limit in force, `LLM_NUM_PREDICT`, and
  `LLM_EXTRA_BODY`. `EMPTY_SQL_RESPONSE` stays for its own case — a model
  that stopped cleanly and said nothing — which may well differ on a
  retry, so that path still retries. Both the v2 conversational path and
  `/query` make the distinction.

- **The web UI no longer looks signed out while holding a valid key.** The
  key field is cleared after a save, so in live mode an empty password box
  sat in the topbar permanently — which is what a page shows when you are
  signed *out*, and analysts read it that way and re-entered a key they
  already had. With a key stored, the control now collapses to
  `کلید: ذخیره شده ✓` plus a "تغییر کلید" button; the entry field appears
  only when there is no key, or when the server has rejected the one
  stored. Same change in the admin panel, where it was worse: an empty
  login box beside ten loaded cards.

- **A 401 no longer destroys the stored key.** The raw key is printed
  exactly once by `scripts/issue_api_key.py` and is not recoverable, and a
  401 is not always the key's fault — a server restarted with a different
  `API_KEYS_JSON`, or an application database briefly unreachable, produces
  one from a perfectly good key. The UI kept clearing it, turning a
  transient server condition into "find that 43-character string again".
  The key is now kept and marked rejected, with the entry field revealed
  so a genuinely bad key can still be replaced.

- **The RTL topbar no longer pushes its own controls off the screen.**
  Neither half of the bar could shrink below its content width (no
  `min-width: 0`), so instead of wrapping it overflowed — and in RTL that
  overflow runs off the *left* edge into negative coordinates, where
  nothing can scroll to it. Below roughly 1400px this silently put the
  API-key controls and the mode switch outside the window. Both halves now
  shrink and wrap, and the collapsed key control removes ~300px of width
  that was causing it in the first place.

---

## [4.9.0] — 2026-09-08

### Added

- **`LLM_EXTRA_BODY` — server-specific fields, passed through verbatim.**
  A JSON object merged into every chat-completions request body. Empty by
  default, which sends exactly what this project has always sent.

  It exists for one concrete failure. A reasoning model spends its
  completion budget thinking before it answers, so with `LLM_NUM_PREDICT`
  at its 512-token default a Qwen3-class model can consume every token on
  reasoning and be cut off before emitting a single character of SQL. That
  arrives as `EMPTY_SQL_RESPONSE` — a description of the response, not of
  the cause — after the correction loop has retried it twice.

  Turning that reasoning off is not expressible in the OpenAI
  chat-completions schema, and every server spells it differently: vLLM and
  SGLang take `chat_template_kwargs.enable_thinking`, Ollama takes `think`,
  OpenAI takes `reasoning_effort`. Encoding those dialects here would mean
  claiming to know every inference server's private vocabulary and silently
  sending the wrong key whenever the guess was wrong. The passthrough says
  the honest thing: these are your server's fields.

  ```ini
  LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
  ```

  Applies to the structured (`response_format: json_schema`) path too — a
  constrained decode is still a decode, and a reasoning model asked for
  JSON reasons first.

  Keys that would let one environment variable contradict the settings the
  `llm` status block reports are refused at parse time rather than
  overridden at merge time: `model`, `messages`, `temperature`, `top_p`,
  `seed`, `max_tokens`, `stop`, `stream`, `n`. The refusal names the
  setting to use instead. A malformed value fails in `Settings.validate()`
  — so `scripts/verify_deployment.py` and the server's own start-up gate
  catch it, rather than an analyst's first question.

### Documentation

- **`LLM_NUM_PREDICT` is documented in `.env.example`.** It never was, so
  the one setting that explains an empty response from a reasoning model
  was discoverable only by reading `config.py`. The entry names the
  signature to look for in the audit record — `finish_reason: "length"`,
  `completion_tokens` exactly equal to the cap, `reasoning_detected: true`,
  empty `generated_sql` — because that combination does not look like a
  token limit from the UI, which reports only that the model returned
  nothing.

---

## [4.8.0] — 2026-09-07

The admin panel can manage keys and roles. Until now it could not, and
nothing said so: `POST /admin/keys`, the disable/enable/revoke routes, the
ACL route and the role routes have all existed since phase 2 with no UI
calling any of them, so issuing a key or granting a role meant the CLI —
and a revocation phase 2 deliberately made *immediate* was, in practice,
immediate once someone reached a terminal.

### Added

- **A "کلیدها و دسترسی‌ها" section in the admin panel.** Lists every key
  with its state, source (`.env` or the panel), and column restriction;
  issues new keys; disables, re-enables and revokes them; edits a key's
  `denied_columns`; and grants or revokes either admin role. Each control
  is gated server-side exactly as its route always was — `operations` for
  the key lifecycle, `security` for anything that changes what a key can
  see — and the panel never guesses at that gate client-side: it makes the
  call and reports the server's own answer.

  Three things the routes alone do not make visible, which this section
  states at the moment each one matters:

  - A key issued through the API starts with **every** column denied. It
    authenticates and can read nothing until a security admin loosens it.
    That split is deliberate, but an operations admin who does not know it
    has just handed someone a key that looks broken.
  - Disable is reversible; revoke is not, and they are one button apart.
    Revoke asks for the key's id to be typed rather than confirmed — every
    row's buttons sit in the same place, and a confirm dialog defends
    against not reading, not against clicking the right button on the
    wrong row.
  - The raw key exists exactly once, in the issue response. The reveal is
    not written to `localStorage`, not put in the URL, and never
    re-rendered by a later refresh.

- **A 403 on a capability-gated card renders inside that card.** The
  panel's sections do not share one capability, so a key holding only some
  of the three legitimately sees a 403 on the rest. That previously raised
  the page-level "this key is not an admin key" banner — false, and
  pointing at the wrong fix. The keys card now names the missing flag.

### Fixed

- **Revoking a role granted through `API_KEYS_JSON` is refused instead of
  silently doing nothing.** `appdb.key_store.get_active_principals` unions
  the environment's capabilities back into every resolved principal on
  every load, so deleting a row from `admin_principal_roles` cannot remove
  one — there may be no row at all. The route returned
  `200 {"granted": false}` and the next read of the holders still listed
  the principal. It is now a 409 naming the only thing that does work:
  remove the flag from that entry and restart.

  Found by the new panel section, which is the first caller that made this
  route clickable. The last-holder rule still takes precedence when both
  apply — losing a capability entirely is the larger fact, and that error
  already points at `API_KEYS_JSON` and a restart.

### Changed

- `web/admin/admin.js`'s `_get` and `_post` were two hand-written copies
  of the same auth-and-error block. Phase 7 needed a third verb, and a
  third copy would have meant three places for the 401/403 distinction to
  drift apart — a distinction the panel depends on to tell "enter a key"
  from "this key lacks a capability". Both are now thin wrappers over one
  `_request`, alongside the new `_patch`.

- The panel header no longer claims the page only reads. That stopped
  being true in phase 4.

---

## [4.7.0] — 2026-09-07

### Added

- **`scripts/issue_api_key.py` can grant all three admin capabilities.**
  `--operations` and `--security` join `--admin`, plus `--full-admin` for
  all three at once. All three fields have been parseable by
  `security/auth.py` since the admin panel's phase 2, but only `--admin`
  was issuable — so granting the other two meant hand-editing the
  `API_KEYS_JSON` array, which is exactly the "raw key and digest get
  confused" territory this script exists to keep operators out of.

- **The script warns when asked for a capability set that produces a
  partly-403 admin panel.** The panel's ten sections do not sit behind one
  capability: `require_admin` serves four of them, and
  `require_operations_or_security` the other six. A key holding only
  `admin` therefore loads a panel where more than half the sections report
  a permissions error — which reads as a broken deployment rather than as
  a decision someone made at issue time. Both partial shapes are now named
  at issue time, with the flag that fixes them.

### Fixed

- **The raw key is no longer the easy half of the output to miss.** This
  script prints two strings that go to two different places: the raw key
  to a browser field, the JSON entry to `.env`. 4.6.1 added a 401 hint for
  operators who pasted the digest into the key field, on the reasoning
  that the entry is "the conspicuous, copy-pasteable artefact" — correct,
  and aimed one step too late. The entry was conspicuous *because this
  script made it so*, printing both values as equally-weighted flat lines
  under similar-looking headings; operators reported not registering that
  a second value existed at all.

  The raw key is now framed, indented, and labelled with its length and
  its destination, and the two are numbered as sequential steps rather
  than listed as outputs. Plain ASCII, no colour: this output is routinely
  piped, redirected, and pasted into tickets.

- **`VERIFY_API_KEY`'s usage hint is spelled for the shell it is printed
  into.** It read `VERIFY_API_KEY=<raw key> ...` — the POSIX inline-
  environment form, which PowerShell does not have. On Windows, where
  `docs/fa/getting-started.md` walks through every setup step in
  `powershell` blocks, pasting it produced `The term 'VERIFY_API_KEY=...'
  is not recognized as a name of a cmdlet` — which reads as "this script
  is broken", the opposite of what a check whose whole job is building
  confidence should do. The hint now branches on the platform.

### Documentation

- **The capability-to-panel-section mapping is written down**, in
  `docs/fa/getting-started.md` §2.5.1, `docs/deployment-runbook.md` §1.1,
  and `.env.example`. It was previously derivable only by reading the
  `Depends(...)` on each route, which is why "the panel is half 403" had
  no answer anywhere in the documentation.

- **`docs/fa/getting-started.md` §0.4 says what the pre-flight check does
  not prove.** Without `VERIFY_API_KEY` it confirms that `API_KEYS_JSON`
  parses and holds at least one key — not that *your* key authenticates.
  Passing it while the browser returns 401 was a coherent state with no
  explanation in the guide.

- Every `python scripts/issue_api_key.py` invocation in the docs is now
  `python -m scripts.issue_api_key`, matching the form the setup guide has
  used throughout. README's step 4 also no longer shows the command
  without its two required arguments.

---

## [4.6.1] — 2026-09-06

### Fixed

- **A 401 now says so when the presented credential is a SHA-256
  digest.** `scripts/issue_api_key.py` prints two things: the raw key,
  once, under a line saying it will not be shown again — and then an
  `API_KEYS_JSON` entry containing that key's `key_sha256`. The entry is
  the conspicuous artefact: it is what goes into configuration, what gets
  copied into a ticket, and what is still on screen after the "copy this
  now" line has scrolled away.

  So presenting the digest at the key field is an easy and repeatable
  mistake, and it was one this system said nothing about — hashing a hash
  finds no match, and "Missing or invalid API key" is indistinguishable
  from a typo, a revoked key, or a key meant for another deployment.

  The reverse mistake has been a loud start-up error since phase 8, where
  `key_sha256` is checked against a 64-hex pattern. This is the same
  courtesy in the direction an operator actually hits, where there is no
  start-up left to fail. The hint cannot fire on a legitimate key:
  `secrets.token_urlsafe(32)` is 43 characters of URL-safe base64 and can
  never be 64 hex characters.

### Documentation

- **`docs/fa/getting-started.md` names the two traps the key-issuance
  flow sets.** Which of the two printed values goes to the browser and
  which to `.env`, the lengths that tell them apart at a glance, and a
  one-line command that hashes a raw key so it can be compared against
  what is configured. Plus the `.env` one: a multi-line `API_KEYS_JSON`
  must be wrapped in single quotes, because `.env` is line-based and
  dotenv otherwise takes `[` as the whole value and reads the remaining
  lines as new keys — a silent failure a long way from "the server will
  not start".

---

## [4.6.0] — 2026-09-06

Phases 3 through 6 of `docs/admin-panel-architecture.md`. The panel is
complete, and that document's "still to be decided" section is empty.

### Added

- **`project_config/` is editable, versioned, and reversible.** The nine
  YAML files live in the application database as full bundle snapshots —
  never diffs, so restoring any depth is a read rather than a replay.
  Restoring an old version always creates a *new* one: intervening
  history is never touched, and the restore is itself reversible.

  `schema.yaml` stays the security admin's. An operations save that
  touches it is held as an unapplied draft until a security admin
  approves or rejects it; the other eight apply immediately. Every save
  is validated through the real loaders, diffed — naming exactly which
  tables and columns enter and leave the guard's allowlist — and dry-run
  against the golden set before it can apply. A save names the version it
  was based on and is refused, not merged, if that version has moved on.

- **A channel for "this answer was wrong".** The audit trail records what
  happened *technically*; a query that executed cleanly and returned a
  plausible wrong number was indistinguishable from a correct one in
  every artefact this system produced. The analyst-facing flag control
  lives in `web/`, because an analyst is the only person who knows.

  The feedback row deliberately holds no question, no SQL and no result
  rows — those columns do not exist. Both are already in the audit log
  under the same ids, so triage joins to them rather than duplicating
  them into a second place to keep consistent and a second place to leak
  from.

  Nothing auto-applies, structurally: `appdb/feedback.py` does not import
  the configuration write path. A promoted golden case is written
  `pending_expected` and the regression gate skips it entirely until
  someone supplies the expectation — a promoted case that silently joined
  the baseline with a wrong expectation would make the gate enforce the
  bug.

- **Moving the application database between backends.**
  `python -m scripts.migrate_app_db`. Starting on SQLite is only safe if
  leaving it is supported, and leaving is the expected path. Identifiers
  are preserved exactly — renumbering would turn audit and feedback
  references into dangling ones with nothing failing at the time. After
  copying, both sides are re-read and compared per table by row count and
  content hash; a migration that cannot prove equality is a failed
  migration and says so. The source is never mutated, and its hash is
  printed before and after every run, including a failed one.

- **The operational tier.** Maintenance mode; a read-only schema-drift
  comparison against the live warehouse; vocabulary freshness with manual
  refresh; per-analyst usage and rate-limit pressure; cache controls.

  Maintenance mode is a switch rather than a trap: the panel stays
  reachable, `/health` and `/` stay open, analyst queries get a 503 with
  a body instead of a hang, and in-flight requests drain. Both properties
  fall out of *where* the check is placed — a dependency on the routes
  that submit a query and on nothing else — rather than from an exemption
  list a new route could be forgotten from.

  Schema drift proposes and never applies: `schema.yaml` is asserted
  byte-identical after a check that finds differences.

### Changed

- **Failed authentication gets its own rate-limit bucket.** Keys are 256
  bits, so guessing one is arithmetically impossible and tightening the
  shared limit for that reason would be theatre. The real gap was
  structural: authentication runs before the limiter, so the `ip:` bucket
  held auth failures *together with* the unauthenticated traffic a
  monitoring probe lives in — one client looping on a stale key could
  starve the probe sharing its address, and the resulting 429 reads
  exactly like an outage. Failures now bucket separately and small, and
  every failure is recorded with its source address and surfaced, because
  the threat to an admin key is leak rather than guessing.

- **The admin-action trail is retained by time, not size.** It rotated by
  size like every other JSONL log, which discards the oldest evidence
  first — exactly when there is most activity. Anyone wanting to bury one
  action could do it by generating noise. A trail whose purpose is "each
  role can read that the other acted" cannot depend on a mechanism the
  watched party defeats by volume. Configuration versions and feedback
  rows are kept in full: capping rollback depth removes the feature, and
  deleting resolved feedback destroys the trend the loop exists to show.

### Fixed

- **A knowledge-base edit did not move the query cache key.** Only three
  of the nine configuration files feed the static prefix, so editing
  `aliases.yaml` or `retrieval_hints.yaml` changed what the engine
  retrieved — and therefore the SQL it wrote — without moving the
  prefix's content hash. Every answer cached under the old configuration
  kept being served. The shape of that failure was the bad part: an
  operations admin makes the edit, asks the same question to check it,
  and gets the pre-edit answer back with nothing to say why. The cache
  key is now composed with the configuration bundle's own version id.

- **Configuration import was the one unguarded way into a new version.**
  Save, restore and approve all validated, diffed, dry-ran and held a
  `schema.yaml` change from an operations-only caller as a draft. Import
  did none of that when the deployment had no version history yet — and
  since history is created lazily, "no history yet" means "nobody has
  opened the configuration page since this deployment came up", exactly
  when the first admin action is most likely to *be* an import.

- **The migration tool copied nothing, and said so nowhere.** A
  `sqlite_sequence` existence check asked through `inspect(engine)`.
  SQLite engines here use `StaticPool`, so every checkout shares one
  DBAPI connection: that opened a second `Connection` facade over the
  same underlying connection, and closing it returned it to the pool,
  which resets with a ROLLBACK. The rollback landed on the copy's own
  transaction and discarded every row — silently, no exception, no
  warning, `import_export` returning normally. A read-only existence
  check, of a table that does not exist, threw away the entire migration.
  Only the per-table verification caught it.

- **`AuditRecord.as_dict()` silently dropped `session_id` and
  `turn_id`.** 4.2.0 added the fields and never serialised them, which
  would have made the feedback loop's join to the audit log impossible.

### Notes

- Maintenance mode's flag lives in the server process's memory, which is
  what makes its own toggle unblockable — and also means the migration
  tool, a separate process, cannot observe it. That tool keeps its own
  recent-write-activity refusal and says so in its message. Moving the
  flag into the application database is the clearest remaining piece of
  work on the panel, and is recorded as such.
- Exercised against SQLite only. No PostgreSQL or SQL Server was
  reachable, so those backends are implemented and unverified by
  execution.

---

## [4.5.0] — 2026-09-06

### Added

- **An application database.** A new `appdb/` package: one schema, one
  SQLAlchemy layer, several backends. SQLite when `APP_DB_URL` is unset —
  the only genuine zero-configuration fallback, since PostgreSQL is a
  server and cannot be made to appear. Elsewhere, tables are created
  inside a database someone else provisioned, because `CREATE DATABASE`
  needs rights a DBA will not grant an application.

  `session/persistence.py` is ported off raw `sqlite3` onto the same
  layer. Its existing tests pass unchanged, including the byte-level
  check that result rows never reach the file or its WAL.

  Start-up now depends on this database and fails closed when it is
  unreachable, consistent with how every other misconfiguration is
  treated here. The error names *which* database is missing, warehouse or
  application, because those have different fixes.

- **Two admin roles.** Operations holds key lifecycle and domain
  knowledge; security holds `denied_columns`, `schema.yaml`, the
  warehouse connection string, and the granting of either role. The rule
  that decides any future case: anything that changes who can see what
  data belongs to security.

  Each escalation path has a test that fails if it opens — issuing a key
  cannot set its ACL, so operations cannot mint itself an unrestricted
  one; operations cannot grant any role, including to itself; every
  security-gated route refuses an operations key, discovered from the
  route table rather than hand-listed; and `AUTH_REQUIRED=false` confers
  neither role.

- **Admin actions are audited to their own stream**, so admin activity
  does not pollute the analytics the analyst audit log exists for. Each
  record names *which* capability authorised it — otherwise one person
  holding both roles makes the separation invisible to whoever reviews it
  later. Each role can read that the other acted without being able to
  act.

### Changed

- **API keys moved into the database, read at call time.** `API_KEYS_JSON`
  was read at start-up, so a disable button would have taken effect at the
  next restart; for a leaked key, "tomorrow morning" is not an answer.

  That plus an external database is a network round trip per request — two
  correct decisions colliding. The key set is cached with a short TTL *and*
  invalidated explicitly on every revocation, disable and ACL change. Both
  halves are tested, because with only one of them either revocation is
  not immediate or every question pays a hop.

  Revocation writes a tombstone rather than deleting the row. Restoring
  the database to an earlier point would otherwise restore a key revoked
  because it leaked — backup as the cause of a security regression rather
  than the cure.

- **Phase 1's "no `/admin` route accepts a mutating method" rule is
  replaced, not deleted.** That rule was only ever true because phase 1
  had no writes; the guarantee underneath it was never "no writes exist"
  but "no write is ungated". The replacement walks each route's real
  FastAPI dependant tree and asserts every mutating route declares one of
  the role dependencies.

### Fixed

- **The application database must never be the warehouse.**
  `docs/db-hardening.md` specifies a read-only login for the warehouse and
  `database/executor.py` rolls back every transaction. This database needs
  writes, so if the two ever point at the same place, our own writes
  become the mechanism that undoes that posture — silently, from inside
  the process. The two URLs are compared as parsed endpoints, not as
  strings: `localhost` and `127.0.0.1` naming one database is the same
  mistake spelled twice.

  Reviewing that check found it compared database *names*
  case-sensitively. Case sensitivity varies by backend and by platform —
  SQL Server folds under most collations, PostgreSQL folds unquoted names,
  MySQL depends on the host filesystem — so there is no answer to inherit.
  The failure directions decide it: a false positive costs an operator a
  rename and a clear start-up error; a false negative silently undoes the
  read-only posture. It now over-matches on purpose.

### Notes

- Exercised against SQLite only. No PostgreSQL or SQL Server was reachable
  in this environment, so those backends are implemented and unverified by
  execution — the same boundary the multi-dialect work was held to.

---

## [4.4.0] — 2026-09-05

### Changed

- **Live mode is now the default.** A deployment is live and analysts open
  the UI expecting real answers; defaulting to the simulated demo meant
  synthetic figures rendered in the same interface as real ones —
  labelled, but the wrong default once the system is actually serving
  people. Simulated stays fully available, and `?live=0` now selects it
  explicitly, which was not previously possible because it was already
  the default.

  The honest consequence: a first load against an unreachable backend now
  shows an error rather than a working demo.

- **The backend URL left the analyst's top bar.** It is deployment
  configuration, and a wrong value there looks exactly like a dead
  backend. `web/js/config.js` now holds the default and is the one file a
  deployment edits; `?base=` and the persisted value still override.

  The API-key field stays. It is the analyst's own identity — the audit
  trail records `principal_id` and the rate-limit bucket is the
  `(principal, ip)` pair — so removing it would force either a shared key
  or a key embedded in static files anyone can read.

### Added

- **Generated SQL is syntax-highlighted**, with Prism vendored locally
  the way the fonts are, because `web/README.md` states a no-CDN
  requirement. The theme is authored against this app's existing custom
  properties rather than copied, since no upstream Prism stylesheet is
  both offline and theme-aware here. Highlighting is presentation only:
  the copy path reads the `Turn` object, never the DOM, so it still
  yields the exact original SQL.

### Fixed

- **The UI rendered numbers in two digit systems at once.** `chart.js`
  declared both an `en-US` formatter and an `fa-IR` one and used them in
  adjacent tiles — the total in Latin digits beside the point count in
  Persian. `llm-status.js` did the same inside one stat grid, and
  `table.js`, `turn.js`, `sessions.js`, `memory.js` and `data.js` each
  made the call again locally. `table.js`'s `fmtCell`, which formats every
  table cell *and* every chart value label, was Latin while the row count
  beside it was Persian.

  That is not one bug — it is the same decision taken eight times,
  differently, which is why the numbers looked unsettled.

  `web/js/num.js` is now the only place that decides. Persian digits for
  everything a reader reads; Latin survives in one deliberate carve-out
  for values that travel to other systems, because converting those makes
  them wrong at the far end.

  The test asserts the invariant rather than the formatting — within one
  rendered view, numeric strings may not use both digit systems — so it
  survives a future change of digit system. It caught `fmtCell`, which
  the first pass missed.

---

## [4.3.0] — 2026-09-05

### Added

- **An admin panel, read-only.** Phase 1 of
  `docs/admin-panel-architecture.md`: an `admin` capability on
  `Principal`, four `GET` routes under `/admin`, and a dashboard at
  `web/admin/`. Nothing in it mutates state — no key issuance, no ACL
  change, no config edit, no cache invalidation.

  Read-only is the point of a first cut. The architecture's §2.1 spends
  its length on escalation paths that exist only because an admin can
  change things; none apply yet. It needs no application database and no
  role bootstrapping beyond one capability, which makes it safe to ship
  during a deployment — which a write path is not.

  `/admin/summary` calls `scripts/analyze_audit_log.py`'s `build_report`
  directly rather than reimplementing any of it, and defaults to the
  aggregate-safe mode with the report's own `mode` field passed through.
  `/admin/health/checks` runs `verify_deployment`'s checks on demand.

  `AUTH_REQUIRED=false` cannot confer the capability: the escape hatch
  resolves to `ANONYMOUS`, which has none, and the dependency checks the
  capability rather than short-circuiting on "auth is off".

### Fixed

- **The auth-coverage test had stopped covering `/v2` entirely.**
  FastAPI 0.141 represents each `include_router()` as one opaque
  `_IncludedRouter` whose `.path` is `None` instead of flattening its
  sub-routes into `app.routes`. Dispatch is unaffected; introspection is
  not.

  `tests/test_auth.py` walked `app.routes` naively, so discovery had
  silently fallen from *every route* to *the eight registered directly on
  `app`* — zero `/v2/*` routes. The test whose entire purpose is "a route
  added without auth fails automatically" had stopped looking at the
  conversational API, and stayed green because its floor was `>= 8` and
  it still found exactly eight.

  A silently-narrowed security test is worse than a missing one: it
  reports coverage it does not have. Discovery now recurses, and the
  guard names the route families that must be covered rather than
  counting, because a count cannot express "and it is still looking at
  the conversational API". Coverage went from 8 routes to 23.

- **Chart labels were drawn outside their own viewBox** — sinking out of
  sight in some places, running past the edge in others. SVG neither
  clips nor reflows text, so a label near a boundary simply disappears
  and nothing errors.

  Three causes: the ranked bar chart reserved a fixed 60px gutter for a
  value label that can be a rial figure in the billions; the line chart
  anchored its end-of-axis labels `middle` at the extreme positions,
  putting half of each outside on both sides, and drew its focus label
  above a point that is often the maximum; and `xAt` built its span from
  one padding while starting from the other.

  Value labels now move inside the bar when the gutter cannot hold them,
  edge labels flip their anchor inward, and long category names truncate
  with the full text kept in a `<title>`. The new test asserts geometry
  rather than appearance, across hostile inputs and every framing.

---

## [4.2.0] — 2026-09-05

### Added

- **Audit records now carry `session_id` and `turn_id`.** Additive and
  optional: `None` on the `/query` and CLI paths, which have no session.

  Without them the audit trail could say nothing at all about the
  conversational product. A follow-up question and a fresh one looked
  identical in the log, and "this answer was wrong" could not be traced
  back to the turn that produced it or to the turns it refined.

  `docs/admin-panel-architecture.md` names their absence as the
  prerequisite blocking the panel's entire first tier. This shipped
  during a deployment rather than after it because the cost is not
  recoverable: a week of production logs written without these is a week
  permanently blind to the dimension the deployment exists to exercise.

  Both are identifiers this system generated, not user content — the same
  category as `request_id`, and they reveal no more about the warehouse
  than it does.

---

## [4.1.5] — 2026-09-05

### Documentation

- **A review pass over `docs/admin-panel-architecture.md` found five gaps
  in the design as written.** All are now in the document; one was a real
  security hole.

  - **`AUTH_REQUIRED=false` must not confer either admin capability.**
    The escape hatch resolves the caller to `ANONYMOUS`, which has no
    capabilities — so the safe behaviour falls out, *provided the
    implementation checks the capability*. The shortcut that suggests
    itself ("auth is off, let everything through") would hand every
    anonymous caller the ability to rewrite the guard's allowlist.
  - **Tier 1 is not only a panel feature.** The flag control and its
    endpoint live in the analyst UI, so it changes the analyst-facing
    product too — and "promote this to a golden case" has no defined
    destination, because the golden set is a file outside the
    application database and outside the versioning scheme.
  - **Stored column names are not re-checked against a changed ACL.**
    Result rows are never persisted precisely because a stored row
    cannot be re-checked; column names *are* persisted and get no such
    treatment. Weaker than exposing values, but inconsistent with the
    principle we applied to rows.
  - **Maintenance mode was leaned on twice and never defined** — and
    migration safety depends on it stopping writes. It must also keep
    the panel reachable, or enabling it is a one-way door.
  - **"Bind the panel to loopback" contradicts "the panel is a client of
    the API".** The privileged routes live on the same application that
    serves analysts, so restricting the static server restricts nothing.
    Three resolutions are named; picking one is a prerequisite.

- Four further open questions recorded rather than left implicit,
  including that failed admin authentication carries no principal and so
  buckets on IP alone.

---

## [4.1.4] — 2026-09-05

### Documentation

- **`docs/admin-panel-architecture.md`** — the agreed design for an admin
  panel, written as a contract in the spirit of `api-contract-v2.md`:
  decisions with their reasons, so an implementation can be checked
  against intent rather than guessed at. Nothing is implemented yet.

  The parts worth knowing before reading it:

  - **Two admin roles, split by one rule** — anything that changes who
    can see what data belongs to the security admin, everything else to
    operations. Four escalation paths that would have collapsed that
    split are closed explicitly, including two non-obvious ones:
    `schema.yaml` is a data-access change wearing operational clothes
    (it is the guard's allowlist), and `DB_CONNECTION_URL` could be
    pointed at a more privileged login on the same database.
  - **The split holds because enforcement lives in the guard, not the
    prompt.** That is why editing domain knowledge is safely operational
    — a crafted few-shot example still cannot reach a denied column — and
    why `schema.yaml` is the exception.
  - **Three kinds of data, three homes.** Metadata to a managed database
    (durability and stewardship), the audit log to append-only files
    (tamper-evidence — a DBA must not be able to edit it), secrets to the
    environment.
  - **Configuration versioning is a table, not git**, with revert
    semantics, bundle versions plus per-file restore, and validated
    rollback. Git remains as an *output*: the YAML bundle is exported on
    every version, so an operator's own repository still gives offline
    inspection and off-box backup.
  - **Migration between backends is a shipped tool**, not a property of
    using SQLAlchemy. Verified by row counts and content hashes, never
    mutating the source, requiring maintenance mode, refusing a
    schema-version mismatch.
  - Four prerequisites in today's code are named: `AuditRecord` has no
    `session_id`, `Principal` has no role concept, `API_KEYS_JSON` is
    read at start-up so revocation cannot be immediate, and
    `session/persistence.py` is hardcoded to `sqlite3`.

---

## [4.1.3] — 2026-09-05

### Documentation

- **The setup guide listed `OPENAI_API_KEY` among four values to fill
  in, which reads as required. It is not.** `Settings.validate()` demands
  only `OPENAI_MODEL` and `DB_CONNECTION_URL`, and `.env.example` has
  always shipped the LLM key empty — because most local
  OpenAI-compatible servers (LM Studio, Ollama, llama.cpp) check no
  credentials at all.

  So on a local-model deployment there is exactly **one** real token in
  the whole system: the analyst's key. The three-row table in the guide
  shows three *places*, not three secrets.

  Saying otherwise sent people looking for a credential their model
  server never asked for, and invited the genuinely dangerous shortcut of
  reusing the analyst key for it. The guide now states, explicitly, that
  the two must never share a value: they point in opposite directions —
  `OPENAI_API_KEY` is what this server presents *outward* to the model,
  the analyst key is what a browser presents *inward* to this server —
  and many model servers log the `Authorization` header they receive.

- `tests/test_local_model_needs_no_llm_key.py` pins the claim rather than
  leaving it to drift. Three things in three different modules have to
  hold for that advice to be true: `validate()` must not demand the key,
  the provider must omit the header when it is empty, and the health
  probe must do the same. The last of those was false until 4.1.2.

---

## [4.1.2] — 2026-09-05

### Fixed

- **The LLM health light showed red while the CLI answered questions
  through the same endpoint.** The probe disagreed with the engine in two
  ways, and either alone was enough.

  It **sent an `Authorization` header even with no key configured**,
  building `Bearer ` with nothing after it. Many self-hosted
  OpenAI-compatible servers check no credentials, so an empty
  `OPENAI_API_KEY` is legitimate — and a malformed header is not the same
  as an absent one: plenty of servers answer the first with 401 and the
  second with 200. `llm.providers.OpenAIBackend` omits the header
  entirely when the key is empty, which is exactly why the engine
  succeeded where the probe failed. Both now use the same rule.

  It also **judged an endpoint the engine never calls**. Generation goes
  to `/chat/completions`; the probe asks `/models`, because a liveness
  check must be cheap and must not spend tokens. That trade is fine, but
  it means a 404 says only that this server lacks an endpoint we do not
  use — not a fault, and no longer reported as one. A 401 or 403 is the
  opposite and stays a failure, because the endpoint is reachable and has
  actively refused our credentials.

- **A red light now says why.** `GET /health` gained `openai_detail` and
  `database_detail` (additive, optional), and the UI shows them in the
  indicator's tooltip. "Unreachable host", "wrong key" and "this server
  does not implement `/models`" were three different problems with three
  different fixes and one indistinguishable symptom.

### Documentation

- `docs/fa/getting-started.md` explains why the setup asks for something
  token-shaped in three places, which is a fair thing to find confusing:
  they are **two** secrets, not one entered three times. `OPENAI_API_KEY`
  is the credential for the *model endpoint* and is unrelated to the
  other two; the analyst's key exists as a hash on the server and as the
  raw value in the browser, which is how every password system works —
  storing the raw key would let anyone who reads `.env` impersonate any
  analyst and make the audit trail's `principal_id` worthless.

  It also points at `AUTH_REQUIRED=false`, which removes the analyst key
  entirely for a single-user local run, and says plainly what that costs:
  the audit log can no longer say who asked what, and rate limiting sees
  everyone as one caller.

---

## [4.1.1] — 2026-09-05

### Fixed

- **The server had no route at `/`,** so the first thing anyone does
  after starting it — open `http://localhost:8000` in a browser —
  returned `{"detail":"Not Found"}`. With `APP_DOCS_PUBLIC=false` (the
  default) `/docs` is behind authentication too, so a correctly running,
  correctly configured server looked completely dead to the one check a
  person actually performs. That cost a real deployment an investigation
  into a server that was working perfectly.

  `GET /` now answers, unauthenticated, with the service identity and a
  directory of paths — and, most importantly, says that **the browser UI
  is a separate static server on a different port**. That single
  confusion is what every wrong turn on a first run traces back to.

  It is deliberately boring: no model name (the disclosure `/health`
  already withholds from anonymous callers), no connection string, no
  configuration, no counts. `tests/test_auth.py` pins both halves — open
  without credentials, and leaking nothing — and the route-coverage test
  that enumerates the live route table now carries `/` in an explicit
  `_OPEN_ROUTES` set, so opening a route stays a recorded decision rather
  than something that happens by omission.

- **The version the API reported was `1.0.0`** while the project was at
  4.1.0. Harmless only while nothing read it; `GET /` publishes it. There
  is now one source (`core/version.py`) and a test that reads the newest
  `## [x.y.z]` heading out of this file and fails if the two disagree —
  the failure being prevented is exactly "two places, one updated".

- **Two SQLite sidecar files were committed.** `.gitignore` had `*.db`,
  which matches `sessions.db` but **not** `sessions.db-wal` or
  `sessions.db-shm` — and the write-ahead log is where recently-written
  pages live, so on a deployment that has served real questions it is the
  file most likely to hold questions and generated SQL not yet
  checkpointed into the main database.

  The two files that reached the repository contained only the empty
  schema — no session ids, no turns, no SQL — because the session store
  was disabled in every run that produced them. The gap that let them in
  was real regardless, and would have mattered on a machine that had
  served a week of traffic.

  `.gitignore` now covers the sidecars, and
  `tests/test_no_runtime_artifacts_tracked.py` fails the build both if
  such a file is tracked and if the ignore rules stop covering it. The
  warning explaining this hazard was already in `.gitignore`, written for
  the rotated audit logs; it did not stop the same mistake one line below
  it, because a comment cannot fail a build.

### Documentation

- `docs/fa/getting-started.md` said "two terminals" but let a reader
  treat the second as optional. It now states which port the UI is on,
  that the backend's own port shows nothing in a browser, and that
  terminal 2 is required — with the health check shown through `curl`
  rather than a browser.

---

## [4.1.0] — 2026-09-05

Many conversations instead of one, and preferences that outlive a
session.

### Added

- **A conversation index that survives a restart.** `GET /v2/sessions`
  lists the caller's conversations with titles, recency and turn counts;
  `PATCH /v2/sessions/{sid}` renames one. Sessions, turns and the
  refinement sidecar persist to SQLite (`session_store_path`, default
  `logs/sessions.db`) for `session_retention_days`.

  This separated three lifetimes that had been two: the prompt window
  (`session_prompt_turns`), the live in-memory context
  (`session_ttl_seconds`), and retention. TTL expiry now **demotes** a
  session out of memory instead of deleting it — previously a
  conversation vanished after thirty idle minutes, which is why there was
  nothing worth listing.

- **Cross-session memory.** `GET/PUT/DELETE /v2/memory` — standing
  preferences from a closed set declared in
  `project_config/memory_policy.yaml`. `docs/api-contract-v2.md` §10 had
  deferred this explicitly ("needs authentication first; building it
  before authz means building it wrong"); Phase 8 landed authentication,
  so §10 is now amended rather than deleted, saying what unblocked it and
  what stays out of scope.

  Memory is **explicit only** — an entry exists because the analyst
  pinned it, nothing is inferred from repetition. It surfaces through the
  existing contract rather than as a hidden channel: `Assumption.source`
  gains `"memory"`, editable, so the chips show it and
  `PATCH …/assumptions` overrides it for one turn without changing what
  is stored. Precedence is `question > session > memory > default`;
  `policy` is overridable by none of them.

- **`TurnResult.rows_omitted`** — additive, defaults false. Set on a turn
  rehydrated from disk, so a client can never render a restored turn as
  an empty result: `row_count` stays accurate and this says why the rows
  are absent.

- The web UI gained a conversation sidebar (switch, rename, delete, and
  the last session restored on reload), a memory panel, and a pin control
  on editable assumption chips — the only way memory is created.

- `scripts/verify_deployment.py` checks the session store is writable,
  beside the audit-log check it already performed.

### Security

- **Result rows are never written to disk.** Persistence stores the
  question, the SQL, result **column names**, `row_count` and the
  refinement sidecar. Not the rows. Beyond keeping warehouse data out of
  a file outside the DBA's control, the decisive reason is that a
  persisted row cannot be re-checked against a changed ACL: a principal's
  `denied_columns` can gain a column after the row was written, and no
  guard work at query time catches that, because no query runs. Verified
  at the byte level in both the SQLite file and its WAL.

- **Memory is re-checked against the ACL at read time**, not only when
  written. An entry naming a column the principal may no longer see is
  dropped for that turn and reported in `Turn.warnings` — not applied,
  and not silently ignored either.

- **The query cache partitions on memory that actually applied.**
  Memory-derived filters change the answer, so `scope_key` includes them
  — but only the entries that influenced *this* query. Hashing the whole
  memory set would partition the cache per principal and discard
  cross-user sharing for anyone who ever pinned anything.

### Fixed

- **The shutdown segfault, properly this time.** 4.0.1 made abandoned
  workers daemons, which stops `concurrent.futures` from joining them
  during finalisation. That was real but insufficient: a daemon still
  *running* when finalisation begins is killed at its next GIL
  acquisition, and if that lands inside a C extension call the process
  dies. Being a daemon means the interpreter is allowed to cut the thread
  off; it was never a claim that doing so is safe. Background threads are
  now drained at test-session end, while the interpreter is healthy and a
  join is an ordinary operation. The `PYTHONFAULTHANDLER` and
  thread-reporting added in 4.0.1 are what identified the second source
  by name.

- **Pinning used a chip's display label as the memory key.** Those are
  different things — the label is what the chip shows, the key is a
  config identifier — so every pin would have failed against the real
  backend. The test that covered it picked a fixture where the two
  strings coincided and then asserted against its own input.

- The README described the project as it was at 3.2.0, and printed the
  real warehouse schema in every example. Examples are now a neutral
  illustration; the quick start now mentions `project_config/`, without
  which the server does not start.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — session
  persistence, conversation index, cross-session memory, web UI, and the
  shutdown fixes

---

## [4.0.1] — 2026-09-04

### Fixed

- **A segmentation fault during interpreter shutdown, reported as a bare
  exit code 139 underneath a green test summary.** `resolve_value`'s
  deadline is deliberately soft — on a breach the caller stops waiting
  and the query keeps running with its result discarded — but the query
  ran in a module-level `ThreadPoolExecutor`, whose worker threads are
  **not** daemons. `concurrent.futures` registers an `atexit` hook that
  joins every one of them, so each breached deadline left a thread
  guaranteed to be joined during interpreter finalisation, running
  caller-supplied code against a process whose module globals were
  already being torn down.

  The work now runs in a daemon thread — the same conclusion
  `retrieval/dimension_vocabulary.py` had already reached for its own
  background refresh, and whose comment argues against exactly the pool
  this removes. An abandoned daemon is cut off at exit rather than
  joined.

  Python 3.12 was not special as an interpreter: it is the only version
  whose CI workflow runs the whole suite a second time for coverage, so
  it had two chances per run at the race.

### Added

- `resolve_value_max_concurrency` (`RESOLVE_VALUE_MAX_CONCURRENCY`,
  default 8) — the in-flight bound the removed pool provided as
  `max_workers`, now read at call time. Waiting for a free slot spends
  the caller's own `resolve_value_timeout_seconds`: a saturated resolver
  reports a miss on time rather than queuing past its deadline.
- `PYTHONFAULTHANDLER=1` in CI, so a finalisation crash dumps the
  faulting stack instead of a bare exit code.
- `pytest_sessionfinish` names every thread still alive when a test run
  ends and flags any that is not a daemon. It does not fail the run —
  lingering daemons here are deliberate — but the next crash of this
  shape starts from a thread name instead of a guess.

---

## [4.0.0] — 2026-09-04

Everything between 3.2.0 and here: authentication, the conversational
session API, the evaluation harness, LLM observability, the separation of
domain data from engine code, multi-dialect support, and two web clients.
Two of those changes are breaking.

### Breaking

- **Every route except `/health` now requires authentication.** Requests
  must carry `Authorization: Bearer <key>`. Keys are hashed with SHA-256
  (deliberately not bcrypt or argon2 — these are high-entropy tokens, not
  passwords) and compared with `hmac.compare_digest`. Startup fails
  closed if `AUTH_REQUIRED` is on and no key is configured. Issue keys
  with `python -m scripts.issue_api_key`.

- **Domain data no longer lives in the repository.** `project_config/` is
  gitignored and mandatory: `aliases.yaml`, `business_rules.yaml`,
  `entities.yaml`, `examples.yaml`, `metrics.yaml`, `schema.yaml`,
  `retrieval_hints.yaml`, `session_policy.yaml`. A missing file raises
  `ConfigNotFoundError` at start-up. There is deliberately **no** silent
  fallback to `project_config.example/` — running a real warehouse on
  sample business rules would produce confidently wrong SQL, which is
  worse than refusing to start. An existing deployment will not boot
  until those files are placed by hand.

- `/health` no longer reports the model name to unauthenticated callers.

### Added

- **Conversational sessions (`/v2/sessions…`)** — the `Turn` contract in
  `docs/api-contract-v2.md`, SSE streaming, declared assumptions with a
  source and an editability flag, `PATCH …/assumptions` to re-run a turn
  under edited assumptions, and CTE-composed refinement so «از بین
  آن‌ها» resolves against the previous turn rather than re-querying the
  warehouse.
- **Evaluation harness (`eval/`)** — golden set, execution accuracy,
  per-tag breakdown, error taxonomy, latency percentiles, an
  order-insensitive result fingerprint so "the same answer" is decidable,
  determinism measurement against a live endpoint, and a baseline
  regression gate with a CI exit code.
- **LLM status block** — 21 fields on every response and audit record:
  token counts, prefix-cache-hit ratio, timings, correction count,
  `finish_reason` read from the response rather than assumed, and
  detection of a model answering on its reasoning channel.
- **Multi-dialect support** — `SQL_DIALECT` selects the target; the model
  still generates T-SQL, which is transpiled and then **re-validated by
  the guard in the dialect it will actually execute in**. `tsql` and
  `sqlite` are verified by end-to-end execution; `postgres` and `mysql`
  transpile and re-validate cleanly but are unverified by execution and
  are not claimed. Per-dialect data lives in a `DialectProfile` registry
  (`security/dialects.py`), not in branches.
- **Static web client (`web/`)** — Persian, RTL, no build step: per-turn
  pipeline view, assumption chips, LLM status panel, result-shape
  selection between chart and table driven by the declared column types,
  chart defaults following Storytelling with Data, and export.
- **Flask web app (`webapp/`)** — bilingual FA/EN, sample-question panel,
  SQL beautifier, result pagination, copy and download.
- **Value resolution from the warehouse** (`retrieval/value_resolver.py`)
  with a stale-while-revalidate prefetch and single-flight refresh,
  replacing per-request lookups.
- **Guard rejection taxonomy** — `CorrectableRejection` versus
  `PolicyRejection`, with refusal tracked as an independent axis, so the
  model is no longer re-prompted for rejections no rewrite could satisfy.
- **One canonical Persian normalizer** (`core/persian.py`), versioned, so
  the cache and the retriever agree on what the same question is.
- **Observability** (`observability/`) — compliance-grade audit records
  that never contain result rows, stage timings, and the status block
  above.
- Operator tooling: `scripts/verify_deployment.py`,
  `scripts/issue_api_key.py`, `scripts/analyze_audit_log.py`.
- Documentation: `docs/api-contract-v2.md`,
  `docs/deployment-runbook.md`, `docs/db-hardening.md`, and bilingual
  tutorials under `docs/fa/` and `docs/en/`.
- Licence provenance notices (`core/provenance.py`, `NOTICE`) — stated
  where the licence is actually encountered rather than only in a file
  nobody opens.
- `tests/test_no_domain_literals.py` — walks the AST of first-party
  source and fails if a warehouse name reappears in an executable
  literal. This is what keeps the separation above from decaying.
- `project_config.example/` and `eval_data.example/` templates, so the
  suite and CI run with no real data present.
- CI across Python 3.11, 3.12 and 3.13, with doctests, coverage and an
  offline evaluation gate; branch protection on `main`.

### Changed

- **Prompt assembly is split into a byte-identical static prefix and a
  variable suffix**, so a local endpoint can reuse the prefix's KV cache.
  Per-turn content — session context, resolved filters, correction text —
  goes only in the suffix. This is enforced by tests, because breaking it
  is silent: the suite stays green and every request pays full prefill.
- All domain knowledge moved from Python modules to YAML loaded through
  `knowledge/config_loader.py`, and the guard's table and column
  allowlists are now derived from `schema.yaml` rather than hardcoded.
- `ensure_top` keeps its byte-identical text-splicing path for T-SQL and
  uses an AST row cap for the other dialects.
- The LLM layer reduced to a single OpenAI-compatible provider
  (`llm/providers.py`) behind a router with fallback and bounded retries.
- Rate limits raised from 60 to 600 requests per window and moved into
  `Settings`.
- The suite is now 2,079 tests, up from the 427 the README badge
  advertised at 3.2.0.

### Fixed

- **The rate-limit bucket keyed on the principal alone, then on the IP
  alone.** Either half by itself collapses distinct callers into one
  bucket — every analyst behind one UI host, or every request from one
  key. It is now the `(principal, ip)` pair.
- **The web UI sent no `Authorization` header**, so every route but
  `/health` returned 401 and the only thing the UI could reach was the
  liveness probe. The suite was green throughout because all of it was
  server-side.
- **`denied_columns` reached the query cache's scope key but never
  `validate_sql`** — the column ACL partitioned the cache without
  enforcing anything. It is now threaded through both the `/query` and
  the `/v2/…/turns` generation paths.
- **T-SQL `N'…'` literals and `'a' + 'b'` concatenation transpiled
  completely unchanged.** SQLite rejects the first as a syntax error and
  silently evaluates the second to `0` — an error and a plausible wrong
  number respectively. Both are now refused for non-T-SQL targets.
- Persian presentation forms and Arabic/Persian letter variants made the
  cache and the retriever disagree about the same question.

### Security

- Per-principal column ACL, enforced in the guard and not merely
  partitioned around in the cache.
- Audit records carry `principal_id` and never carry result rows.
- The guard parses to an AST and works from a closed allowlist; the
  bypass suite is parametrised over every claimed dialect, because a
  guard proven for one dialect and assumed for another has unknown holes.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — engine,
  security layer, conversational sessions, evaluation harness,
  observability, multi-dialect support, static web client, domain
  separation
- [**Melika Bahmanabadi**](https://github.com/MelikaBahmanabadi) — Flask
  web application, bilingual FA/EN system, UI work, schema knowledge

---

## [3.2.0] — 2026-06-12

License, documentation, and attribution release.

### Added

- **`LICENSE`** — Project is now licensed under the **Business Source License 1.1 (BUSL-1.1)**.
  - Free for non-production use.
  - Commercial/production use requires a written agreement with the author (Ali Sadeghi Aghili).
  - Converts automatically to Apache 2.0 on **2029-01-01**.
  - Includes an explicit **Attribution Requirement**: any derivative work must retain
    `LICENSE` and display: *"Based on Local SQL Agent by Ali Sadeghi Aghili —
    https://github.com/alisadeghiaghili/local-sql-agent"*.

- **`docs/tutorial.md`** — Comprehensive Persian-language tutorial covering:
  architecture overview, installation, first query end-to-end, TF-IDF retrieval
  internals, prompt building, SQL security pipeline, adding new tables/synonyms,
  miss-analysis workflow, test writing examples, health check, and troubleshooting.

- **`README.md`** — Updated to reflect BUSL-1.1 license, added license badge,
  attribution notice, contributors table, and link to `docs/tutorial.md`.
  `docs/tutorial.md` added to architecture tree.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — Creator & Lead Maintainer

---

## [3.1.0] — 2026-06-12

Minor release — FastAPI HTTP layer, SQLAgent auto-correct loop, LRU query cache,
tested LLM backend abstraction, and 1 bugfix in cache isolation.

### Added

- **`api/`** — FastAPI HTTP service package:
  - `server.py` — FastAPI app factory with `/query`, `/health`, `/cache/stats`,
    `/cache/invalidate`, `/cache/clear` endpoints. `_system_prompt` module-level
    variable allows test-time injection without environment changes.
  - `runner.py` — `run_query()`: cache-aware orchestrator that consults
    `query_cache` before calling `_agent.run`; populates cache on miss.
  - `query_cache.py` — `QueryCache`: thread-safe TTL + LRU in-process cache;
    `set / get / invalidate / clear / stats / reconfigure` API.
    `reconfigure()` now clears the store when TTL or max-size changes.
  - `models.py` — Pydantic `QueryRequest` / `QueryResponse` request/response
    models; `mode` field validates `"full" | "sql" | "result"`.
  - `errors.py` — `NLQError` hierarchy: `OutOfScopeError` (422),
    `ModelTimeoutError` (504), `ModelUnavailableError` (503),
    `QueryExecutionError` (500); FastAPI exception handlers registered
    for each type.
  - `middleware.py` — `RequestLoggingMiddleware`: per-request correlation ID
    (`X-Request-Id`), latency header (`X-Response-Time-Ms`), structured log.
  - `health.py` — `/health` endpoint: probes SQL Server connectivity and
    Ollama reachability; returns per-component status dict.

- **`llm/base.py`** — `LLMBackend` abstract base class + `SQLGenerationResult`
  dataclass (`sql`, `raw_response`, `attempt`, `correction_prompts`).

- **`llm/sql_agent.py`** — `SQLAgent`: wraps any `LLMBackend`; runs the
  generate → `clean_sql` → `validate_sql` loop with up to N correction
  attempts, feeding validation errors back into the prompt.

- **`llm/ollama_backend.py`** — `OllamaBackend(LLMBackend)`: HTTP client
  extracted from the old monolithic `ollama_client.py`; exponential back-off
  retry, `OUT_OF_SCOPE` sentinel detection, `ModelUnavailableError` mapping.

- **`config.py`** — `override_settings()` context manager added for
  test-time settings mutation without environment side-effects.

- **Test files added:**
  `test_api_endpoints.py`, `test_api_runner.py`, `test_cache_endpoints.py`,
  `test_errors.py`, `test_middleware.py`, `test_ollama_backend.py`,
  `test_query_cache.py`, `test_runner_cache.py`, `test_sql_agent.py`.
  Total test count: **427+** (up from 231).

### Fixed

- **`api/query_cache.py` — `reconfigure()` did not clear stale entries.**
  After calling `reconfigure(ttl_seconds=60)`, entries stored under the
  previous TTL could survive and be returned as valid hits.
  Fix: `reconfigure()` now calls `self._store.clear()` after updating
  `_ttl` and `_max_size`.

- **`tests/test_api_runner.py` — cache pollution between tests.**
  Added `autouse` fixture that calls `query_cache.clear()` before and after
  each test, preventing cache hits from earlier tests masking failures in
  later ones (e.g. `test_exception_translated` receiving a cached success).

- **`tests/test_query_cache.py` — `TestQueryCacheRunnerIntegration.test_second_call_hits_cache`
  triggered real Ollama connection.**
  The test called `reconfigure()` inside `override_settings()`, which (after
  the fix above) cleared the pre-populated cache entry. `run_query` then fell
  through to the real `OllamaBackend` → `ModelUnavailableError`.
  Fix: removed `reconfigure()` / `override_settings()` from the test; the
  cache is pre-populated with `query_cache.set()`, `_agent` is patched with
  a `MagicMock` that raises on `.run()`, and the returned object is asserted
  to be the exact cached instance. Added companion test
  `test_cache_miss_calls_agent_and_stores_result` for the miss path.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — FastAPI layer, SQLAgent, OllamaBackend, QueryCache, middleware, error taxonomy, test suite expansion

---

## [3.0.1] — 2026-06-11

Bugfix release — 12 failing tests resolved across three independent areas.

### Fixed

- **`schema_data/registry.py`** — `SchemaRegistry.build_context()` alias added;
  `None` and empty-tuple arguments now treated identically to "include all tables".
  Resolves `AttributeError: type object 'SchemaRegistry' has no attribute 'build_context'`
  (7 tests in `test_schema_registry.py`).

- **`schema_data/retriever.py`** — `_IdfDict.get()` now overrides `dict.get` to
  return `_max_idf` for unseen terms instead of the caller-supplied default.
  `dict.__missing__` is only invoked on `[]` access, not `.get()`, so the earlier
  implementation silently returned `0` for any token absent from the corpus.
  Also added `fallback: bool = True` parameter to `retrieve_tables()`; when
  `False`, an empty list is returned instead of the full table list when no
  table scores above `_MIN_SCORE`. Used by `analyse()` to avoid false negatives.
  Resolves `test_rare_term_has_higher_idf` and `test_detects_miss_when_table_not_retrieved`.

- **`schema_data/tables.py`** — Persian translations appended to every description
  so that common Persian terms (e.g. `معامله`, `مشتری`, `عرضه`) appear in the
  IDF corpus as seen terms with a finite IDF, while truly unseen terms receive
  the strictly higher `_max_idf`. This is required for `test_rare_term_has_higher_idf`
  to be meaningful.

- **`scripts/analyze_misses.py`** — `_KNOWN_TOKENS` now built from three sources:
  `TABLE_DESCRIPTIONS` values, `SYNONYMS` keys, **and** `SYNONYMS` values.
  Previously only descriptions were scanned; synonym expansion terms such as
  `مشتری` were therefore not recognised as known and appeared in the candidate
  list. Resolves `test_filters_existing_description_tokens`.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — retrieval bugfixes, IDF override, schema registry alias, miss-analysis token scan

---

## [3.0.0] — 2026-06-11

Full architectural consolidation. All feature branches merged into `main`.
Legacy `schema/` package retired. Modular retrieval pipeline fully activated.

### Added

- **`core/models.py`** — `RetrievalContext` frozen dataclass: single shared contract
  between the retrieval layer and `PromptBuilder`. Fields: `entities`, `facts`,
  `dimensions`, `relationships`, `business_rules`, `examples`, `filters`.
  Convenience properties: `selected_tables` (order-preserving dedup) and `is_empty()`.
- **`core/__init__.py`** — package marker.
- **`knowledge/aliases.py`** — `SYNONYMS` dict (156 entries, 10 categories: temporal,
  trade, customer, offer, ring/hall, commodity, broker, logistics, finance,
  aggregation) added alongside the existing `RING_ALIASES`. Resolves
  `ImportError: cannot import name 'SYNONYMS'` that blocked all 5 test files.
- **`schema_data/retriever.py`** — TF-IDF bigram engine migrated from the retired
  `schema/retriever.py`; now the canonical fallback for `EntityRetriever` and
  `FactRetriever`. Unchanged logic, corrected import path.
- **`knowledge/`** — full knowledge base promoted from `develop` branch:
  `entities.py` (entity catalog), `examples.py` (20+ tagged few-shot SQL examples),
  `metrics.py` (metric definitions), `business_rules.py` (expanded, topic-keyed rules).
- **`retrieval/`** — modular retrieval pipeline promoted from `develop` branch:
  `EntityRetriever`, `FactRetriever`, `RelationshipRetriever`, `RuleRetriever`,
  `ExampleRetriever` (tag-overlap scoring, 20+ bilingual tags), `ValueRetriever`
  (ring canonical lookup + Persian year regex).
- **`schema_data/`** — schema package promoted from `develop` branch:
  `registry.py` (LRU-cached `build_schema_context`), `columns.py`, `tables.py`,
  `relationships.py` (FK edge → JOIN SQL map).
- **`prompt_engine/`** — `PromptBuilder` and `PROMPT_TEMPLATE` promoted from
  `develop` branch; replaces inline string construction in `ollama_client.py`.

### Changed

- **`retrieval/context_retriever.py`** — `from core.models import RetrievalContext`
  now resolves correctly. `selected_tables` uses `dict.fromkeys()` for
  order-preserving deduplication (replaces non-deterministic `list(set())`).
- **`llm/ollama_client.py`** — pipeline comment updated to reflect final
  module paths (`retrieval.context_retriever`, `prompt_engine.builder`).
- **`tests/test_retriever.py`** — import updated to `schema_data.retriever`.
- **`tests/test_schema_registry.py`** — import updated to `schema_data.registry`.
- **`scripts/analyze_misses.py`** — import updated to `schema_data.retriever`.

### Removed

- **`schema/`** package fully retired (8 files deleted):
  `retriever.py`, `schema_registry.py`, `synonyms.py`, `tables.py`,
  `table_schemas.py`, `relationships.py`, `business_rules.py`, `__init__.py`.
  All functionality migrated to `schema_data/` and `knowledge/`.
- **`develop` branch** — merged and deleted.
- **`prompt-accurated` branch** — ancestor of `main`, no unique changes; deleted.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — full architectural consolidation, modular retrieval pipeline, knowledge base, schema migration

---

## [2.0.0] — 2026-06-06

### Added
- `config.py`: typed `Settings` dataclass (frozen, `__slots__`), `validate()` method, singleton via `lru_cache`
- `database/connection.py`: `dispose_engine()` helper for test teardown and hot-reload
- `database/executor.py`: wraps `SQLAlchemyError` in `RuntimeError`; debug-logs row/column counts
- `llm/ollama_client.py`: calls `clean_sql()` on every model response before returning
- `security/sql_guard.py`: **new** `clean_sql()` (strips markdown fences, preamble prose, converts LIMIT→TOP, fixes SELECT TOP n DISTINCT order); **new** `ensure_top()` (inject TOP n when missing); extended `_FORBIDDEN` list (`EXECUTE`, `XP_`, `SP_`); `LIMIT` now blocked in `validate_sql()`
- `logs/query_log.py`: `Literal["SUCCESS", "ERROR", "OUT_OF_SCOPE"]` status type; `as_dict()` serialisation method; `__slots__`
- `logs/logger.py`: uses `settings.log_dir`; catches `OSError` on write failure
- `exporters/excel_exporter.py`: uses `ExcelWriter` context manager; auto-fits column widths (capped at 60)
- `schema_data/retriever.py`: bigram scoring (bigram match counts ×1.5); `_MIN_SCORE` threshold; `_ALWAYS_INCLUDE` forced-table logic
- `schema_data/registry.py`: `lru_cache(maxsize=64)` on `build_schema_context()`; accepts `tuple` for cache-safe API
- `app.py`: structured REPL with emoji indicators; separates `RuntimeError` from `ValueError`; logs elapsed time; prints row-count summary; graceful `KeyboardInterrupt` / `EOFError` shutdown
- `tests/test_config.py`: rewritten for new `Settings` dataclass
- `tests/test_sql_guard.py`: **new** — full coverage of `clean_sql`, `validate_sql`, `ensure_top`

### Changed
- `config.py`: flat module-level constants replaced by `Settings` dataclass + `settings` singleton
- All modules now import `from config import settings` (single import point)
- `schema_data/registry.py`: `build_schema_context` takes `tuple[str, …]` for LRU-cache compatibility

### Removed
- `tests/test_validator.py`: replaced by `tests/test_sql_guard.py`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — config refactor, SQL guard, exporter improvements, REPL, test coverage

---

## [1.1.0] — 2026-06-06

### Added
- Modular NLQ engine: `database/`, `exporters/`, `llm/`, `logs/`, `prompts/`, `security/`
- `app.py` entry point replacing `main.py`
- `config.py` with env-var-only configuration (no hardcoded credentials)
- `security/sql_guard.py`: read-only SQL validator
- Initial `schema/`: table registry, column schemas, FK relationships, business rules, TF-IDF keyword retriever
- `prompts/`: system prompt, business glossary, few-shot examples
- `.env.example` with all required variables documented
- `README.md` added

### Removed
- Legacy root scripts: `main.py`, `nlq.py`, `langsql.py`, `langchain_sql.py`, `nlq_with_sqlite.py`, `prompt_based_nlq.py`, `simple_nlq.py`, `create_db.py`
- Legacy folders: `agents/`, `src/`
- Old config files: `CHANGELOG` (plain text), `pyproject.toml`, `ruff.toml`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — initial modular architecture, CLI, security layer, schema package

---

## [1.0.0] — initial

### Added
- Proof-of-concept scripts for NLQ-to-SQL via Ollama + LangChain
- `scripts/create_db.py`: SQLite sample database for local testing
- Initial `agents/`, `runners/`, `tests/` structure

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — proof-of-concept, initial structure
