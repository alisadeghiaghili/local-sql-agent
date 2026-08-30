"""Offline/online evaluation harness for the NL->SQL engine.

This package measures **execution accuracy** — whether a generated SQL
query, when executed, returns the same *data* as a hand-verified reference
query — rather than comparing SQL strings.  Two syntactically different
queries (different join order, different alias names, ``COUNT(*)`` vs
``COUNT(1)``) can both be correct; string diffing cannot tell them apart,
so this harness never does it.

Modules
-------
models
    Typed records shared by every other module: :class:`~eval.models.GoldenCase`,
    :class:`~eval.models.CaseResult`, :class:`~eval.models.EvalReport`.
fingerprint
    Canonical, order-insensitive hashing of a :class:`pandas.DataFrame` result
    set.  This is the load-bearing module: it is what lets two differently
    shaped-but-equal result sets compare equal.
runner
    Executes a golden set against injected ``generate_fn`` / ``execute_fn``
    callables, in either offline (replay, CI-safe) or live (real DB + LLM)
    mode.
report
    Builds and renders a human-readable + JSON report from a list of
    :class:`~eval.models.CaseResult`.
baseline
    Persists a report as a baseline and compares a new run against it,
    for CI regression gating.
determinism
    Probes whether a *live* generator produces byte-identical SQL for the
    same question across repeats -- a per-endpoint property, not a global
    assumption. Refuses to run against offline replay, which would report
    100% determinism unconditionally by construction.
cli
    ``python -m eval.cli run --golden <path> [--live] [--baseline <path>]``

Design constraints
-------------------
* No module in this package opens a network connection, a database
  connection, or imports an LLM backend at **import time**.  Anything that
  needs a real database or a real LLM endpoint is constructed lazily,
  inside a function, only when *live* mode is explicitly requested.
* Every public function/class is fully type-annotated (Python 3.11+).
"""

from __future__ import annotations
