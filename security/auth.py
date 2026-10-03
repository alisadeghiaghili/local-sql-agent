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
stored (the ``key_sha256`` field of ``API_KEYS_JSON`` / ``API_KEYS_FILE``)
or held in memory here.
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
import threading
from dataclasses import dataclass, field
from pathlib import Path
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
    """The API key configuration (``API_KEYS_JSON`` or the ``API_KEYS_FILE``
    file) is unreadable, not valid JSON, set twice, or an entry is malformed."""


# Keyed on (id, key_sha256), not the entry's index, so reordering the array
# does not re-warn but a new key, or a new hash under the same id, does.
_warned_missing_denied_columns: set[tuple[str, str]] = set()
_warned_missing_denied_columns_lock = threading.Lock()


def _should_warn_missing_denied_columns(principal_id: str, key_sha256: str) -> bool:
    """Whether this key entry has not yet been warned about in this process.

    ``load_api_keys`` re-parses the key text on every call, including
    every ``appdb.key_store`` cache refresh while serving, so warning per
    parse repeats the same lines indefinitely and buries real log output.
    The first call for an ``(id, key_sha256)`` pair returns ``True`` and
    records it; later calls return ``False``.

    Parameters
    ----------
    principal_id:
        The entry's ``id``.
    key_sha256:
        The entry's normalized (stripped, lowercase) ``key_sha256``.

    Returns
    -------
    bool
        ``True`` exactly once per distinct pair.

    Examples
    --------
    >>> _reset_denied_columns_warnings()
    >>> _should_warn_missing_denied_columns("a", "0" * 64)
    True
    >>> _should_warn_missing_denied_columns("a", "0" * 64)
    False
    >>> _should_warn_missing_denied_columns("a", "1" * 64)
    True
    >>> _reset_denied_columns_warnings()
    """
    pair = (principal_id, key_sha256)
    with _warned_missing_denied_columns_lock:
        if pair in _warned_missing_denied_columns:
            return False
        _warned_missing_denied_columns.add(pair)
        return True


def _reset_denied_columns_warnings() -> None:
    """Forget every key already warned about. For tests only."""
    with _warned_missing_denied_columns_lock:
        _warned_missing_denied_columns.clear()


class _DuplicateJsonKeyError(ValueError):
    """An object in the key JSON repeats a field name."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json.loads`` ``object_pairs_hook`` that refuses a repeated key.

    ``json.loads`` keeps the last of two identical keys and says nothing, so
    a pasted second ``"denied_columns"`` would silently replace the first on
    a security ACL. Same refusal as ``core.yaml_loading.safe_load_strict``
    for the YAML configuration.
    """
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateJsonKeyError(key)
        out[key] = value
    return out


def _parse_api_keys(raw_json: str, source: str = "API_KEYS_JSON") -> dict[str, Principal]:
    """Parse the API key JSON array into ``{key_sha256_hex_lowercase: Principal}``.

    Parameters
    ----------
    raw_json:
        The raw JSON text, from ``API_KEYS_JSON`` or from the file named by
        ``API_KEYS_FILE``. Empty / whitespace-only returns
        an empty mapping rather than raising — "no keys configured" is a
        valid (if, under ``AUTH_REQUIRED=true``, fatal-at-startup) state,
        not a parse error.
    source:
        What to call *raw_json* in error and warning messages, e.g.
        ``"API_KEYS_JSON"`` or ``"API_KEYS_FILE (project_config/api_keys.json)"``
        (see :func:`api_keys_source`).

    Raises
    ------
    ApiKeyConfigError
        If *raw_json* is present but not a JSON array of objects each
        carrying a well-formed ``id``, ``name``, and ``key_sha256`` — see
        the per-field checks below — or repeats a field name inside one
        object. Every rejection here is deliberate:
        this is a security ACL, and guessing at a malformed operator's
        intent (coercing a bare string to a list, silently keeping the
        first of two colliding keys, ...) is how a config typo becomes a
        silent access-control bug instead of a startup failure someone
        actually sees. Messages carry positions and field names, never the
        text being parsed.

    Examples
    --------
    >>> _parse_api_keys("")
    {}
    >>> _parse_api_keys('{"id": "a"}', "API_KEYS_FILE (keys.json)")
    Traceback (most recent call last):
        ...
    security.auth.ApiKeyConfigError: API_KEYS_FILE (keys.json) must be a JSON array of key objects
    >>> _parse_api_keys('[{"id": "a", "id": "b"}]')
    Traceback (most recent call last):
        ...
    security.auth.ApiKeyConfigError: API_KEYS_JSON repeats the key 'id' inside one object -- the later value would silently win
    """
    if not raw_json or not raw_json.strip():
        return {}

    try:
        entries = json.loads(raw_json, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ApiKeyConfigError(
            f"{source} is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except _DuplicateJsonKeyError as exc:
        raise ApiKeyConfigError(
            f"{source} repeats the key {exc.args[0]!r} inside one object -- "
            "the later value would silently win"
        ) from exc

    if not isinstance(entries, list):
        raise ApiKeyConfigError(f"{source} must be a JSON array of key objects")

    keys: dict[str, Principal] = {}
    seen_ids: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ApiKeyConfigError(f"{source}[{i}] must be a JSON object")
        try:
            principal_id = entry["id"]
            name = entry["name"]
            key_sha256 = entry["key_sha256"]
        except KeyError as exc:
            raise ApiKeyConfigError(
                f"{source}[{i}] is missing required field: {exc}"
            ) from exc

        if not isinstance(principal_id, str) or not principal_id:
            raise ApiKeyConfigError(f"{source}[{i}].id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ApiKeyConfigError(f"{source}[{i}].name must be a non-empty string")
        if not isinstance(key_sha256, str) or not key_sha256:
            raise ApiKeyConfigError(
                f"{source}[{i}].key_sha256 must be a non-empty string"
            )

        normalized_hash = key_sha256.strip().lower()
        if not _SHA256_HEX_RE.match(normalized_hash):
            raise ApiKeyConfigError(
                f"{source}[{i}].key_sha256 must be a 64-character SHA-256 "
                "hex digest (hashlib.sha256(raw_key.encode()).hexdigest()) -- "
                "never the raw key itself. Got a value of the wrong shape."
            )

        raw_denied = entry.get("denied_columns")
        if raw_denied is None:
            denied_columns: tuple[str, ...] = ()
            # Finding 6 (2026 audit): a key issued through the admin panel
            # gets appdb.key_store._maximally_restrictive_denied_columns()
            # -- deny-all by default. An env-configured key that simply
            # omits this field gets the opposite: NO restriction at all,
            # because `()` denies nothing. That asymmetry is defensible
            # (flipping this default would silently revoke column access
            # from every existing deployment mid-upgrade -- see
            # docs/api-contract-v2.md's API_KEYS_JSON section), but being
            # silent about it is not: an operator who forgot the field,
            # rather than deciding against it, would have no way to find
            # out short of reading this source file. An entry that writes
            # `"denied_columns": []` has made the decision explicitly and
            # does NOT warn -- see the branch below. Once per key per
            # process: this runs on every key-cache refresh, not just at
            # startup.
            if _should_warn_missing_denied_columns(principal_id, normalized_hash):
                logger.warning(
                    "%s[%d] (id=%r) has no denied_columns field -- "
                    "this principal gets NO column restriction. A panel-issued "
                    "key would default to deny-all; an env-configured key does "
                    "not. Set \"denied_columns\": [] explicitly to silence this "
                    "warning once the unrestricted access is intentional.",
                    source, i, principal_id,
                )
        elif isinstance(raw_denied, list) and all(isinstance(c, str) for c in raw_denied):
            denied_columns = tuple(raw_denied)
        else:
            # Deliberately NOT coerced (e.g. a bare "Price" string would
            # otherwise silently become ('P','r','i','c','e') via
            # tuple("Price") -- an ACL that looks configured but denies
            # nothing, with no error anywhere). The operator must write a
            # JSON array of column-name strings.
            raise ApiKeyConfigError(
                f"{source}[{i}].denied_columns must be a JSON array of "
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
                f"{source}[{i}].admin must be a JSON boolean (true/false) "
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
                    f"{source}[{i}].{field_name} must be a JSON boolean "
                    f"(true/false) -- got {type(raw_value).__name__}"
                )
            if raw_value:
                capability_flags.append(capability)

        capabilities = frozenset(
            ([ADMIN_CAPABILITY] if is_admin else []) + capability_flags
        )

        if principal_id in seen_ids:
            raise ApiKeyConfigError(
                f"{source}[{i}].id {principal_id!r} duplicates the id "
                f"already used by entry {seen_ids[principal_id]} -- a "
                "repeated id makes the audit trail ambiguous about who ran "
                "a query"
            )
        if normalized_hash in seen_hashes:
            raise ApiKeyConfigError(
                f"{source}[{i}].key_sha256 duplicates the hash already "
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


#: Repository root, used to resolve a *relative* ``API_KEYS_FILE`` the way
#: ``PROJECT_CONFIG_DIR`` is (``knowledge.config_loader._REPO_ROOT``), not
#: against the process's working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Read once per process, like an environment variable: an edit needs a
# restart, and a key-cache refresh can never see a half-written file. Keyed on
# the resolved path so a changed ``API_KEYS_FILE`` is read afresh.
_api_keys_file_text: dict[Path, str] = {}
_api_keys_file_lock = threading.Lock()


def _reset_api_keys_file_cache() -> None:
    """Forget every ``API_KEYS_FILE`` already read. For tests only."""
    with _api_keys_file_lock:
        _api_keys_file_text.clear()


def _api_keys_file_path() -> Path | None:
    """The resolved ``API_KEYS_FILE``, or ``None`` when it is unused."""
    configured = cfg.settings.api_keys_file.strip()
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_absolute() else _REPO_ROOT / path


def api_keys_source() -> str:
    """What the key configuration is called in messages, without reading it.

    Returns
    -------
    str
        ``"API_KEYS_FILE (<path as configured>)"`` when ``API_KEYS_FILE`` is
        set, otherwise ``"API_KEYS_JSON"``.

    Examples
    --------
    >>> import config
    >>> with config.override_settings(api_keys_file=""):
    ...     api_keys_source()
    'API_KEYS_JSON'
    >>> with config.override_settings(api_keys_file="project_config/api_keys.json"):
    ...     api_keys_source()
    'API_KEYS_FILE (project_config/api_keys.json)'
    """
    configured = cfg.settings.api_keys_file.strip()
    return f"API_KEYS_FILE ({configured})" if configured else "API_KEYS_JSON"


def _read_api_keys_file(path: Path) -> str:
    """The text of the key file at *path*, read at most once per process.

    Raises
    ------
    ApiKeyConfigError
        If the file is missing, unreadable, or not UTF-8 text. The message
        names the resolved path and never any file content. Only a
        *successful* read is remembered.
    """
    with _api_keys_file_lock:
        cached = _api_keys_file_text.get(path)
        if cached is not None:
            return cached
        try:
            # utf-8-sig: Windows editors and PowerShell's ``Out-File`` add a
            # BOM that ``json.loads`` would reject on a str.
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            # No ``str(exc)``: it quotes the offending bytes.
            raise ApiKeyConfigError(
                f"API_KEYS_FILE {path} is not valid UTF-8 text (byte offset {exc.start})"
            ) from exc
        except OSError as exc:
            raise ApiKeyConfigError(
                f"API_KEYS_FILE {path} could not be read: {exc.strerror or type(exc).__name__}"
            ) from exc
        _api_keys_file_text[path] = text
        return text


def load_api_keys() -> dict[str, Principal]:
    """Parse the configured API keys at call time.

    The key text comes from the file named by ``cfg.settings.api_keys_file``
    when that is set, otherwise from ``cfg.settings.api_keys_json``; both set
    is refused, because two sources would leave it unclear which one an
    operator's edit was meant to change. ``API_KEYS_JSON`` is read through
    ``cfg.settings`` on every call (never cached), matching this project's
    existing configuration contract so that ``config.override_settings()``
    patches are visible immediately — see ``config.py``'s module docstring.
    The file is read once per process, like an environment variable: edit it
    and restart.

    Raises
    ------
    ApiKeyConfigError
        Both sources set, the file unreadable, or the key text malformed —
        see :func:`_parse_api_keys`.
    """
    path = _api_keys_file_path()
    if path is None:
        return _parse_api_keys(cfg.settings.api_keys_json)
    if cfg.settings.api_keys_json.strip():
        raise ApiKeyConfigError(
            "Both API_KEYS_JSON and API_KEYS_FILE are set -- use one. Remove "
            "API_KEYS_JSON from .env (or the environment) to keep the file, "
            "or clear API_KEYS_FILE to keep API_KEYS_JSON."
        )
    return _parse_api_keys(_read_api_keys_file(path), api_keys_source())


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


def scope_key(principal: Principal, memory_used: Mapping[str, str] | None) -> str:
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
        caller's whole stored memory set. ``None`` omits memory from the
        key entirely, unchanged from Phase 8's original behaviour before
        §5 added this parameter.

        Deliberately **required, with no default** (Finding 8, 2026
        audit): every call site existing when this parameter was added
        omitted it, which was harmless only because nothing yet cached
        along the memory-aware path — see the ``# SECURITY`` note at
        ``session/engine.py``'s "future T0 cache tier" marker for exactly
        where that stops being true. A default that is silently correct
        today and silently wrong the day someone wires that tier is worse
        than no default: whoever adds that caller would inherit
        ``None`` by doing nothing, and two analysts with different pinned
        memory would share a cache entry neither of them can see is
        shared. Forcing every caller to write ``memory_used=None`` (when
        that really is the answer) or the real mapping (when it is not)
        makes "no memory was used" a stated fact instead of an assumption.

        Memory-derived filters change the answer, so two principals with
        different stored preferences must never share a cache entry for a
        query their memory actually influenced — but hashing the *entire*
        memory set regardless of relevance would partition the cache per
        principal for anyone who ever pinned anything, even on queries
        their memory never touched, throwing away exactly the cross-user
        sharing this function exists to preserve.

    Examples
    --------
    >>> scope_key(Principal(id="a", name="A"), memory_used=None) == \\
    ...     scope_key(Principal(id="b", name="B"), memory_used=None)
    True
    >>> scope_key(Principal(id="a", name="A", denied_columns=("X",)), memory_used=None) == \\
    ...     scope_key(Principal(id="b", name="B", denied_columns=("X",)), memory_used=None)
    True
    >>> scope_key(Principal(id="a", name="A", denied_columns=("X",)), memory_used=None) == \\
    ...     scope_key(Principal(id="a", name="A", denied_columns=("Y",)), memory_used=None)
    False
    >>> scope_key(Principal(id="a", name="A"), memory_used=None) == \\
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
