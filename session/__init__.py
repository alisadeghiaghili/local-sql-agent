"""Phase 3 — conversation, session memory, and declared assumptions.

See ``docs/api-contract-v2.md`` for the frozen contract this package
implements: :mod:`session.models` (the ``Turn`` shape and request/response
envelopes, §3-§4), :mod:`session.store` (the in-memory, TTL/count/turn-cap
session store, §9), :mod:`session.refinement` and :mod:`session.composer`
(the §2 "among those" semantics), :mod:`session.ambiguity` (§5), and
:mod:`session.engine` (the orchestrator tying all of the above together).
"""
