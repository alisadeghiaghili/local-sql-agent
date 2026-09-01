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

import config as cfg

logger = logging.getLogger(__name__)

#: Minimum raw API key length, enforced at issue time
#: (``scripts/issue_api_key.py``) — not re-checked here, since a key
#: already issued and deployed must keep authenticating even if this
#: threshold is later raised.
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
    """

    id: str
    name: str
    denied_columns: tuple[str, ...] = field(default_factory=tuple)


#: The implicit principal used when ``AUTH_REQUIRED=false`` (the
#: deliberate escape-hatch — see config.py) and no valid key was
#: presented anyway. Carries no column restriction, same as "everyone"
#: before this phase existed.
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


def scope_key(principal: Principal) -> str:
    """The query-cache partition key for *principal* — Phase 8's cache seam.

    Two principals with identical data visibility (``denied_columns``)
    share this key and therefore share cache entries; two with different
    visibility can never collide. This is deliberately **not** the
    principal's own id — keying on id directly would throw away all
    cross-user cache sharing on a shared org tool where most questions
    repeat (see the Phase 8 spec's rationale).

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

    Collision assumption
    ---------------------
    The sorted column names are joined on ``":"`` before hashing, so in
    principle ``("Price:Volume",)`` and ``("Price", "Volume")`` would hash
    identically. This is treated as a non-issue rather than fixed with a
    length-prefixed/JSON encoding: a T-SQL identifier cannot contain a
    colon at all, so ``denied_columns`` (validated as plain column-name
    strings by :func:`load_api_keys`) can never actually contain one.
    """
    joined = ":".join(sorted(principal.denied_columns)) or "all"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
