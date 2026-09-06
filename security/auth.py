# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Principal identity and API-key authentication — Phase 8.

This module is deliberately framework-agnostic: it knows nothing about
FastAPI, Starlette, or HTTP status codes. The ASGI-layer wiring (resolving
a request's ``Authorization`` header, stashing the result on
``request.state``, and turning a missing/invalid principal into a 401)
lives in :mod:`api.auth`. That split is what leaves a clean seam for a
future OIDC/JWT resolver: only ``resolve_principal``/``load_api_keys``
would need to change, :class:`Principal` and every downstream consumer
(``security.sql_guard``'s ``denied_columns``, the query cache's scope
key, the audit trail, session ownership) stay exactly as they are.

Scheme
------
Named API keys, not JWT/OIDC — see ``docs/api-contract-v2.md``'s
authentication section for the rationale. A key is a high-entropy random
token (``scripts/issue_api_key.py`` mints one with
``secrets.token_urlsafe(32)``); only its SHA-256 hex digest is ever
stored (``API_KEYS_JSON``'s ``key_sha256`` field) or held in memory here.
Plain SHA-256 — not bcrypt/argon2 — is the correct primitive: those exist
to slow brute-force guessing of a *low-entropy human password*, which is
not what this is. Entropy is instead enforced structurally, once, at
issue time (:data:`MIN_KEY_LENGTH`).

Comparison is via :func:`hmac.compare_digest` against every configured
key's hash, with no early return on a prefix match — a bug that let a
"starts with the right prefix" token through, or that only checked the
first N configured keys, would defeat the whole scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Mapping

import config as cfg

logger = logging.getLogger(__name__)

#: Minimum raw API key length, enforced at issue time
#: (``scripts/issue_api_key.py``) — not re-checked here, since a key
#: already issued and deployed must keep authenticating even if this
#: threshold is later raised.
#:
#: Deliberately a source constant, not a ``config.Settings`` field: it is
#: an *invariant* of this module's security design (structural entropy
#: enforced once, at issue time), not a per-warehouse/per-hardware tuning
#: knob. Making it env-overridable would let a deployment quietly weaken
#: its own auth by setting one variable. See ``config.py``'s module
#: docstring ("Three layers, not two") for the tuning/invariant/
#: implementation-detail rule this is the canonical invariant example of.
MIN_KEY_LENGTH = 32

_BEARER_PREFIX = "Bearer "

#: A SHA-256 hex digest is exactly 64 lowercase hex characters. Enforced
#: on every ``key_sha256`` entry so a raw key pasted into that field by
#: mistake — which can never authenticate anyway, since it will never
#: equal the hash of whatever the caller presents — is a loud startup
#: error instead of a silent "this principal can never log in", and so
#: that the raw key is never sitting in config at all, which this
#: module's docstring promises never happens.
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


#: The one capability the admin panel's phase 1 slice needs (read-only
#: observability — ``docs/admin-panel-architecture.md`` §2/§3, restricted
#: to the tier-2 "already computed, currently invisible" surface: audit
#: summary, deployment checks, cache stats, config-load counts). The
#: architecture document's eventual two-role split ("operations" vs.
#: "security") would add further entries to :attr:`Principal.capabilities`
#: alongside this one — a principal can already carry any number of them —
#: so that split is additive to this shape, not a rewrite of it.
ADMIN_CAPABILITY = "admin"

#: The admin panel's phase 2 two-role split
#: (``docs/admin-panel-architecture.md`` §2): key lifecycle, domain
#: knowledge, LLM endpoint settings, and everything else that does not
#: change who can see what data.
OPERATIONS_CAPABILITY = "operations"

#: The admin panel's phase 2 two-role split: ``denied_columns`` on any
#: principal, ``schema.yaml``, ``DB_CONNECTION_URL``, and granting either
#: role -- "anything that changes who can see what data" (§2's dividing
#: rule). Deliberately a separate capability from :data:`ADMIN_CAPABILITY`
#: (phase 1's single read-only capability), which continues to gate
#: exactly the routes it always gated -- the two-role split is additive to
#: :attr:`Principal.capabilities`, not a rewrite of it.
SECURITY_CAPABILITY = "security"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller — ``docs/api-contract-v2.md``'s auth section.

    Parameters
    ----------
    id:
        Stable, short identifier. Appears in audit records, session
        ownership, and rate-limit bucket keys — never logged or stored
        as anything derived from the raw key itself.
    name:
        Human-readable label, for logs and operator-facing output only.
    denied_columns:
        Column names this principal must never see. Fed into two
        independent places, both HTTP-only (the CLI/REPL paths --
        ``app.py``, ``llm/wizard_llm.py`` -- have no ``Principal`` and
        call :func:`~security.sql_guard.validate_sql` unchanged):

        1. The query cache's scope key (see :func:`scope_key`), so two
           principals with different visibility can never share a cached
           result.
        2. The existing :func:`~security.sql_guard.validate_sql` ACL seam
           (its ``denied_columns`` parameter) — actually enforced, not
           just partitioned around: ``api.runner.run_query`` threads it
           through ``SQLAgent.run``/``_safe_generate_sql_only`` for the
           ``/query`` path, and ``session.engine.TurnEngine.ask`` threads
           it through both the CTE-refinement and fresh-generation paths
           for ``/v2/sessions/*/turns``. A principal configured with
           ``denied_columns=["NationalID"]`` gets a guard rejection, not
           just a private cache partition, if its generated SQL selects
           that column.
    capabilities:
        Named administrative capabilities this principal holds — empty
        for every ordinary analyst key. Phase 1 of the admin panel
        (``docs/admin-panel-architecture.md``) defines exactly one,
        :data:`ADMIN_CAPABILITY`, checked by :func:`api.auth.require_admin`.
        A set, not a single ``is_admin`` field, precisely so the
        architecture document's later two-role split (operations/security)
        is *additive* here — a second capability name joins the set — not
        a rewrite of this dataclass's shape. Never populated for
        :data:`ANONYMOUS`: the ``AUTH_REQUIRED=false`` escape hatch must
        not confer any capability (see that constant's own docstring).
    """

    id: str
    name: str
    denied_columns: tuple[str, ...] = field(default_factory=tuple)
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        """Whether this principal carries :data:`ADMIN_CAPABILITY`."""
        return ADMIN_CAPABILITY in self.capabilities

    @property
    def is_operations(self) -> bool:
        """Whether this principal carries :data:`OPERATIONS_CAPABILITY`."""
        return OPERATIONS_CAPABILITY in self.capabilities

    @property
    def is_security(self) -> bool:
        """Whether this principal carries :data:`SECURITY_CAPABILITY`."""
        return SECURITY_CAPABILITY in self.capabilities


#: The implicit principal used when ``AUTH_REQUIRED=false`` (the
#: deliberate escape-hatch — see config.py) and no valid key was
#: presented anyway. Carries no column restriction, same as "everyone"
#: before this phase existed, and — just as deliberately — no
#: capabilities: the escape hatch that lets every caller through must
#: never also hand every caller the admin surface (see
#: ``docs/admin-panel-architecture.md`` §2.3).
ANONYMOUS = Principal(id="anonymous", name="anonymous")


class ApiKeyConfigError(ValueError):
    """``API_KEYS_JSON`` is not valid JSON, or an entry is malformed."""


def _parse_api_keys(raw_json: str) -> dict[str, Principal]:
    """Parse ``API_KEYS_JSON`` into ``{key_sha256_hex_lowercase: Principal}``.

    Parameters
    ----------
    raw_json:
        The raw ``API_KEYS_JSON`` string. Empty / whitespace-only returns
        an empty mapping rather than raising — "no keys configured" is a
        valid (if, under ``AUTH_REQUIRED=true``, fatal-at-startup) state,
        not a parse error.

    Raises
    ------
    ApiKeyConfigError
        If *raw_json* is present but not a JSON array of objects each
        carrying a well-formed ``id``, ``name``, and ``key_sha256`` — see
        the per-field checks below. Every rejection here is deliberate:
        this is a security ACL, and guessing at a malformed operator's
        intent (coercing a bare string to a list, silently keeping the
        first of two colliding keys, ...) is how a config typo becomes a
        silent access-control bug instead of a startup failure someone
        actually sees.
    """
    if not raw_json or not raw_json.strip():
        return {}

    try:
        entries = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ApiKeyConfigError(f"API_KEYS_JSON is not valid JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise ApiKeyConfigError("API_KEYS_JSON must be a JSON array of key objects")

    keys: dict[str, Principal] = {}
    seen_ids: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ApiKeyConfigError(f"API_KEYS_JSON[{i}] must be a JSON object")
        try:
            principal_id = entry["id"]
            name = entry["name"]
            key_sha256 = entry["key_sha256"]
        except KeyError as exc:
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}] is missing required field: {exc}"
            ) from exc

        if not isinstance(principal_id, str) or not principal_id:
            raise ApiKeyConfigError(f"API_KEYS_JSON[{i}].id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ApiKeyConfigError(f"API_KEYS_JSON[{i}].name must be a non-empty string")
        if not isinstance(key_sha256, str) or not key_sha256:
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].key_sha256 must be a non-empty string"
            )

        normalized_hash = key_sha256.strip().lower()
        if not _SHA256_HEX_RE.match(normalized_hash):
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].key_sha256 must be a 64-character SHA-256 "
                "hex digest (hashlib.sha256(raw_key.encode()).hexdigest()) -- "
                "never the raw key itself. Got a value of the wrong shape."
            )

        raw_denied = entry.get("denied_columns")
        if raw_denied is None:
            denied_columns: tuple[str, ...] = ()
        elif isinstance(raw_denied, list) and all(isinstance(c, str) for c in raw_denied):
            denied_columns = tuple(raw_denied)
        else:
            # Deliberately NOT coerced (e.g. a bare "Price" string would
            # otherwise silently become ('P','r','i','c','e') via
            # tuple("Price") -- an ACL that looks configured but denies
            # nothing, with no error anywhere). The operator must write a
            # JSON array of column-name strings.
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].denied_columns must be a JSON array of "
                "column-name strings, e.g. [\"NationalID\", \"Phone\"] -- got "
                f"{type(raw_denied).__name__}"
            )

        # Admin panel, phase 1 (docs/admin-panel-architecture.md §2): a
        # single, optional capability flag. Absent means false -- an
        # ordinary analyst key gains no new surface just by this field
        # existing in the schema. `bool` is checked explicitly (not
        # merely truthy) for the same reason `denied_columns` rejects a
        # bare string above: a typo like `"admin": "false"` (a non-empty
        # string, therefore truthy) must fail loudly at parse time rather
        # than silently granting the admin capability.
        raw_admin = entry.get("admin")
        if raw_admin is None:
            is_admin = False
        elif isinstance(raw_admin, bool):
            is_admin = raw_admin
        else:
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].admin must be a JSON boolean (true/false) "
                f"-- got {type(raw_admin).__name__}"
            )

        # Admin panel, phase 2 (docs/admin-panel-architecture.md §2): the
        # two-role split, bootstrapped from the environment the same way
        # phase 1's single "admin" flag is -- "the first admin of each
        # kind comes from the environment, never from a web flow" (§2.3).
        # Each flag is checked the same explicit-bool way as "admin" above,
        # for the same reason: a typo like "security": "false" (truthy,
        # being a non-empty string) must fail loudly at parse time rather
        # than silently granting the security capability.
        capability_flags: list[str] = []
        for field_name, capability in (
            ("operations", OPERATIONS_CAPABILITY),
            ("security", SECURITY_CAPABILITY),
        ):
            raw_value = entry.get(field_name)
            if raw_value is None:
                continue
            if not isinstance(raw_value, bool):
                raise ApiKeyConfigError(
                    f"API_KEYS_JSON[{i}].{field_name} must be a JSON boolean "
                    f"(true/false) -- got {type(raw_value).__name__}"
                )
            if raw_value:
                capability_flags.append(capability)

        capabilities = frozenset(
            ([ADMIN_CAPABILITY] if is_admin else []) + capability_flags
        )

        if principal_id in seen_ids:
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].id {principal_id!r} duplicates the id "
                f"already used by entry {seen_ids[principal_id]} -- a "
                "repeated id makes the audit trail ambiguous about who ran "
                "a query"
            )
        if normalized_hash in seen_hashes:
            raise ApiKeyConfigError(
                f"API_KEYS_JSON[{i}].key_sha256 duplicates the hash already "
                f"used by entry {seen_hashes[normalized_hash]} ({principal_id!r} "
                f"vs {keys[normalized_hash].id!r}) -- two principals sharing one "
                "key hash means whichever is parsed last silently wins, "
                "which can silently replace a column-restricted principal "
                "with an all-access one"
            )
        seen_ids[principal_id] = i
        seen_hashes[normalized_hash] = i

        keys[normalized_hash] = Principal(
            id=principal_id, name=name, denied_columns=denied_columns,
            capabilities=capabilities,
        )
    return keys


def load_api_keys() -> dict[str, Principal]:
    """Parse ``cfg.settings.api_keys_json`` at call time.

    Reads through ``cfg.settings`` on every call (never cached), matching
    this project's existing configuration contract so that
    ``config.override_settings()`` patches are visible immediately — see
    ``config.py``'s module docstring.
    """
    return _parse_api_keys(cfg.settings.api_keys_json)


def load_all_principals() -> dict[str, Principal]:
    """``{key_sha256_hex_lowercase: Principal}`` merged from
    ``API_KEYS_JSON`` and the admin panel's application database (phase 2
    — ``docs/admin-panel-architecture.md`` §5.5/§5.6), cached with an
    explicit-invalidation short TTL.

    A deferred import of :mod:`appdb.key_store` — that module imports this
    one (for :class:`Principal` and :func:`load_api_keys`), so importing
    it back at module scope here would be circular; deferring it to call
    time (this function's own body) breaks the cycle the same way this
    module's docstring already does for
    :func:`~config.Settings.validate`'s dialect imports. This is the one
    function in this framework-agnostic module that knows the application
    database exists at all — see the module docstring's remark that only
    ``resolve_principal``/``load_api_keys`` (now also this function) would
    ever need to change for a future auth backend.
    """
    from appdb.key_store import get_active_principals

    return get_active_principals()


def resolve_principal(
    authorization_header: str | None, keys: dict[str, Principal],
) -> Principal | None:
    """Resolve an ``Authorization`` header value to a :class:`Principal`.

    Parameters
    ----------
    authorization_header:
        The raw ``Authorization`` header value, or ``None`` if absent.
        Only the ``Bearer <key>`` scheme is accepted — ``X-API-Key`` and
        every other transport is deliberately not supported (one way in
        is one thing to reason about).
    keys:
        ``{key_sha256_hex_lowercase: Principal}``, as returned by
        :func:`load_api_keys`.

    Returns
    -------
    Principal | None
        The matching principal, or ``None`` for a missing header, a
        non-``Bearer`` scheme, an empty token, or a token matching no
        configured key (including a token that merely shares a prefix
        with a real one — see the module docstring on comparison).

    Every candidate key hash is compared against the presented token's
    hash with :func:`hmac.compare_digest`; the loop always walks every
    entry rather than returning as soon as a *prefix* looks promising, so
    there is no early-exit signal an attacker could use to narrow down a
    key incrementally.
    """
    if not authorization_header or not authorization_header.startswith(_BEARER_PREFIX):
        return None

    token = authorization_header[len(_BEARER_PREFIX):].strip()
    if not token:
        return None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    match: Principal | None = None
    for known_hash, principal in keys.items():
        if hmac.compare_digest(token_hash, known_hash):
            match = principal
    return match


def scope_key(principal: Principal, memory_used: Mapping[str, str] | None = None) -> str:
    """The query-cache partition key for *principal* — Phase 8's cache seam,
    extended (§5) to also fold in the memory entries that influenced the
    current query.

    Two principals with identical data visibility (``denied_columns``) AND
    identical *memory_used* share this key and therefore share cache
    entries; two differing in either can never collide. This is
    deliberately **not** the principal's own id — keying on id directly
    would throw away all cross-user cache sharing on a shared org tool
    where most questions repeat (see the Phase 8 spec's rationale).

    Parameters
    ----------
    memory_used:
        ``{key: value}`` for exactly the memory entries that actually
        changed this turn's resolved filters (the ``used`` return value of
        :func:`session.memory.apply_memory_to_assumptions`) — **not** the
        caller's whole stored memory set. ``None`` (the default, and every
        pre-§5 call site) omits memory from the key entirely, unchanged
        from Phase 8's original behaviour.

        Memory-derived filters change the answer, so two principals with
        different stored preferences must never share a cache entry for a
        query their memory actually influenced — but hashing the *entire*
        memory set regardless of relevance would partition the cache per
        principal for anyone who ever pinned anything, even on queries
        their memory never touched, throwing away exactly the cross-user
        sharing this function exists to preserve.

    Examples
    --------
    >>> scope_key(Principal(id="a", name="A")) == scope_key(Principal(id="b", name="B"))
    True
    >>> scope_key(Principal(id="a", name="A", denied_columns=("X",))) == \\
    ...     scope_key(Principal(id="b", name="B", denied_columns=("X",)))
    True
    >>> scope_key(Principal(id="a", name="A", denied_columns=("X",))) == \\
    ...     scope_key(Principal(id="a", name="A", denied_columns=("Y",)))
    False
    >>> scope_key(Principal(id="a", name="A")) == \\
    ...     scope_key(Principal(id="a", name="A"), memory_used={"scope": "x"})
    False
    >>> scope_key(Principal(id="a", name="A"), memory_used={"scope": "x"}) == \\
    ...     scope_key(Principal(id="b", name="B"), memory_used={"scope": "x"})
    True

    Collision assumption
    ---------------------
    The sorted column names are joined on ``":"`` before hashing, so in
    principle ``("Price:Volume",)`` and ``("Price", "Volume")`` would hash
    identically. This is treated as a non-issue rather than fixed with a
    length-prefixed/JSON encoding: a T-SQL identifier cannot contain a
    colon at all, so ``denied_columns`` (validated as plain column-name
    strings by :func:`load_api_keys`) can never actually contain one. The
    same reasoning covers *memory_used*: its keys are declared identifiers
    (``project_config/memory_policy.yaml``) and its values are already
    newline/control-character-free (:func:`session.memory.validate_memory_value`),
    so a ``"|"``-joined ``key=value`` encoding cannot collide across two
    genuinely different *memory_used* mappings for any value this codebase
    ever stores.
    """
    joined = ":".join(sorted(principal.denied_columns)) or "all"
    if memory_used:
        memory_part = "|".join(f"{k}={v}" for k, v in sorted(memory_used.items()))
        joined = f"{joined}#mem:{memory_part}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
