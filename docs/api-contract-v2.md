# API contract v2 — conversational sessions

Status: **design, frozen for implementation**. Authored as the shared interface
so the web UI and the backend can be built independently and wired later.

This supersedes the single-shot `POST /query` shape for interactive use.
`POST /query` stays as-is for scripted/one-off callers and stays backward
compatible — v2 is additive.

---

## 1. Why sessions

The single-shot API forces every question to be self-contained. Real analytical
use is iterative:

```
Q1  معاملات مشتری‌های تالار سیمان را نشان بده
    → 342 rows
Q2  از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله
    → refines Q1
Q3  همین را برای سال قبل
    → refines Q2
```

Q2 and Q3 are meaningless without Q1. A session carries that state explicitly
rather than hoping a stateless prompt can reconstruct it.

---

## 2. The two semantics of "among those"

**This is the load-bearing design decision of v2. Read it before implementing.**

When the user says «از بین آن‌ها» / "among those", there are two possible
referents and they return *different answers*:

| Reading | Means | Correct? |
|---|---|---|
| **A — among the rows displayed** | Wrap Q1's SQL, `TOP` included, as a CTE and rank within it | Almost always **wrong**. Q1 was truncated for display at `TOP 100`; the true top-10 by volume may not be in those 100 rows at all. |
| **B — among all rows matching the previous filter** | Reuse Q1's *predicate*, drop Q1's display `TOP`, then rank and take 10 | Almost always what the user means. |

v2 implements **B**, and must state that it did so as an explicit assumption
(see §5). Silently choosing either reading is unacceptable — the two give
different numbers and the user cannot tell which they got.

Composition when `basis.kind == "refines"`:

```sql
WITH _prev AS (
    -- previous turn's SQL, with its outermost display TOP removed
    -- and an inner safety cap of settings.refinement_scan_cap applied
)
SELECT TOP 10 CustomerName, SUM(Volume) AS TotalVolume
FROM _prev
GROUP BY CustomerName
ORDER BY TotalVolume DESC
```

Two hard requirements:

1. The **composed** statement is what the guard validates and what executes.
   Never validate the fragment and then splice it — that is how injection gets
   in through the back door.
2. Removing the previous `TOP` uncaps the inner scan, so a separate
   `refinement_scan_cap` (default: `max_rows_returned * 100`, configurable)
   bounds it. If the inner scan hits that cap, the turn's `warnings` must say
   the refinement was computed over a truncated base — a wrong number the user
   knows about beats a wrong number they don't.

---

## 3. Endpoints

```
POST   /v2/sessions                     → create
GET    /v2/sessions/{sid}               → transcript
DELETE /v2/sessions/{sid}               → drop (and free cached state)
POST   /v2/sessions/{sid}/turns         → ask; returns a Turn
POST   /v2/sessions/{sid}/turns?stream=1 → same, as SSE
PATCH  /v2/sessions/{sid}/turns/{tid}/assumptions → re-run with edited assumptions
GET    /health                          → unchanged
```

Sessions are server-side, in-memory, TTL-bounded, and capped in count — same
discipline as the existing query cache. A session holds at most
`session_max_turns` turns; older turns fall out of the prompt window but stay
in the transcript.

`PATCH .../assumptions` is what makes the assumption chips in the UI
interactive: the client sends back a modified assumption set and gets a fresh
turn. It does not mutate the original turn.

---

## 4. `Turn`

```jsonc
{
  "turn_id": "t_04",
  "session_id": "s_a91f",
  "index": 3,

  "question": "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله",
  "resolved_question": "برای معاملات تالار سیمان در سال ۱۴۰۳، ۱۰ مشتری با بیشترین حجم معامله",

  "basis": {
    "kind": "refines",              // "fresh" | "refines"
    "refines_turn_id": "t_03",
    "composition": "cte",           // "cte" | "none"
    "inherited": ["ring=تالار سیمان", "year=1403"]
  },

  "sql": "WITH _prev AS (...) SELECT TOP 10 ...",
  "sql_display": "SELECT TOP 10 ...",   // optional: flattened, for readability

  "ambiguity": {
    "is_ambiguous": true,
    "assumptions": [
      { "field": "measure",  "value": "حجم معامله (HallMatchingWeight)",
        "source": "question", "editable": true },
      { "field": "scope",    "value": "همهٔ سطرهای منطبق با فیلتر قبلی، نه فقط ۱۰۰ سطر نمایش‌داده‌شده",
        "source": "policy",   "editable": false },
      { "field": "ring",     "value": "تالار سیمان",
        "source": "session",  "editable": true },
      { "field": "period",   "value": "۱۴۰۳",
        "source": "session",  "editable": true }
    ],
    "clarifications": [
      { "field": "measure",
        "prompt": "«برتر» بر اساس کدام معیار؟",
        "options": ["حجم معامله", "ارزش ریالی", "تعداد قرارداد"] }
    ]
  },

  "guard": {
    "verdict": "allowed",           // "allowed" | "rejected"
    "rule": null,                   // populated on rejection
    "injected_top": 10,
    "tables_touched": ["Contract", "Customer", "Ring"]
  },

  "result": {
    "columns": [ { "name": "CustomerName", "type": "string" },
                 { "name": "TotalVolume",  "type": "number" } ],
    "rows": [ ... ],
    "row_count": 10,
    "truncated": false
  },

  "interpretation": "…",
  "tier": "T2",                     // T0 cache | T1 template | T2 single-shot | T3 agent
  "warnings": [],

  "llm": { /* §6 */ },
  "timings": {
    "total_ms": 2840, "plan_ms": 4, "prompt_ms": 11, "llm_ms": 2310,
    "guard_ms": 6, "execute_ms": 480, "interpret_ms": 0
  },

  "error": null                     // { "code": ..., "message": ... } on failure
}
```

`resolved_question` is not cosmetic: it is the system showing its work. If the
user sees «برای معاملات تالار سیمان در سال ۱۴۰۳…» when they meant something
else, they catch the misunderstanding before trusting the number.

---

## 5. Ambiguity policy — answer, then declare

**Never block on a vague question.** Produce the best answer under explicit
assumptions and surface them. Blocking makes the tool feel obstructive, and the
user can correct an assumption far faster than they can phrase a
fully-specified question.

- `is_ambiguous: true` changes *presentation*, not whether a result is returned.
- Every assumption carries a `source`, and the source is shown in the UI:
  - `question` — extracted from what the user actually typed
  - `session`  — inherited from an earlier turn (builds trust in follow-ups)
  - `default`  — a configured fallback
  - `policy`   — a system rule the user cannot override (e.g. the §2 scope rule)
- `clarifications` are *offers*, not gates. The UI renders them as one-click
  refinements that issue a `PATCH`.

The only case that legitimately returns no result: the question cannot be
mapped to the schema under any reasonable assumption. That is
`error.code = "OUT_OF_SCOPE"`, which already exists.

---

## 6. `llm` — the LLM status block

The OpenAI-compatible `/chat/completions` response returns most of this in
its `usage` object; surfacing it costs nothing and is what makes the
Phase 2 latency work measurable.

```jsonc
"llm": {
  "backend": "openai",
  "model": "gpt-oss-20b",
  "endpoint": "http://localhost:8000/v1", // which endpoint answered
  "trusted": true,                  // was that endpoint permitted to see this data
  "endpoint_status": 200,           // HTTP status of the final attempt
  "attempts": 1,                    // >1 means transport retries happened
  "finish_reason": "stop",          // stop | length | schema_violation | error
  "structured_output": true,        // was constrained decoding used

  "prompt_tokens": 4612,            // usage.prompt_tokens
  "completion_tokens": 148,         // usage.completion_tokens
  "prefill_ms": null,               // not distinguished from decode by this protocol
  "decode_ms": null,                // not distinguished from prefill by this protocol
  "total_ms": 2310,                 // measured wall-clock time of the call
  "tokens_per_second": null,        // needs decode_ms, which this protocol has no separate figure for

  "prefix_cache_hit": true,         // derived, see below
  "temperature": 0.0,
  "seed": 7,
  "seed_honored": true,             // endpoint reported system_fingerprint; null when it reports nothing
  "corrections": 0,                 // self-correction rounds spent

  "provider": "openai:gpt-oss-20b", // which backend in the router's fallback chain answered
  "fallback_used": false            // true if the first-choice backend
                                     // failed and a later one answered
}
```

`prefix_cache_hit` is derived, and it is the single most useful number in this
block: on a cache hit `prompt_tokens` collapses to roughly the size of
the variable suffix instead of the full ~4.6k-token prefix. Rule:
`prefix_cache_hit = prompt_tokens < (static_prefix_tokens * 0.5)`.
Record `static_prefix_tokens` per skill version so the ratio stays meaningful.

`seed_honored` is deliberately not a plain assertion of determinism:
`seed`/`temperature`/`top_p` are sent on every request, honoured by
vLLM/llama.cpp, and accepted-without-guarantee by OpenAI's own hosted API.
`seed_honored` reports whether the endpoint's response carried a
`system_fingerprint` — the signal OpenAI's own docs describe for detecting
when a backend change might affect reproducibility — as `true`, or `null`
when the response carries no such signal at all (never `false`: absence
means "unknown", not "confirmed not honoured").

On error, `llm` is still populated as far as it got — a 503 with
`attempts: 3` and `endpoint_status: 0` tells a very different story from a 200
with `finish_reason: "schema_violation"`, and the UI must be able to show that
difference.

---

## 7. SSE event stream

`POST /v2/sessions/{sid}/turns?stream=1` emits, in order:

| Event | Payload | UI effect |
|---|---|---|
| `stage` | `{stage, state}` | drives the pipeline step list |
| `resolved` | `{resolved_question, basis}` | shows what it understood, early |
| `assumptions` | `ambiguity` object | assumption chips appear before the result |
| `sql_delta` | `{text}` | SQL types out live |
| `sql` | `{sql, guard}` | final SQL + guard verdict |
| `rows` | `{columns, rows, row_count}` | table fills |
| `interpretation_delta` | `{text}` | summary types out |
| `llm` | `llm` object | status strip populates |
| `done` | `{turn}` | full Turn for the transcript |
| `error` | `{code, message}` | error banner |

Streaming matters more than raw latency here: `resolved` and `assumptions`
arrive in the first few hundred milliseconds, so the user can tell the system
misunderstood them *before* waiting out the full generation.

---

## 8. Prompt assembly for a follow-up turn

Ordering is deliberate and must not be changed casually — it is what preserves
KV-cache reuse (see architecture Decision 1).

```
[ STATIC PREFIX — byte-identical across every request for a skill version ]
  system prompt
  full schema (types, PKs, FKs)
  relationships
  business rules
  metrics
  few-shot examples
[ /STATIC PREFIX ]

[ VARIABLE SUFFIX — only this changes ]
  session context:
    turn t_03: question, SQL, result columns, row_count
    turn t_02: question, SQL, result columns, row_count
    (last session_prompt_turns turns; SQL + column names only — never row data)
  resolved filters for this turn
  the new question
[ /VARIABLE SUFFIX ]
```

Two rules:

1. **Result columns go in the prompt, result rows do not.** Column names are
   what let the model resolve «از بین آن‌ها»; row data would leak business data
   into the prompt (and into any remote model) for no accuracy gain.
2. The session block sits in the suffix, after the static prefix. Putting
   per-turn content anywhere in the prefix destroys cache reuse and silently
   undoes the entire Phase 2 latency win.

---

## 9. New settings

| Setting | Default | Purpose |
|---|---|---|
| `session_ttl_seconds` | 1800 | idle session expiry |
| `session_max_count` | 500 | cap concurrent sessions (memory bound) |
| `session_max_turns` | 50 | transcript cap per session |
| `session_prompt_turns` | 3 | how many prior turns enter the prompt |
| `refinement_scan_cap` | `max_rows_returned * 100` | §2 inner-scan bound |
| `default_top_n` | `max_rows_returned` | display cap |

All read through `cfg.settings` at call time, per the project's existing
configuration contract.

---

## 10. Out of scope for v2

Deliberately excluded, with reasons:

- **Cross-session memory / user profiles.** Needs authentication first
  (Phase 7). Building it before authz means building it wrong.
- **Session persistence across restarts.** In-memory is correct until there is
  a reason to survive a deploy; adding a store now is unbacked complexity.
- **Agentic multi-step planning inside a turn.** That is Phase 6, tier T3.
  A session is turn-level state, not an agent loop — do not conflate them.
