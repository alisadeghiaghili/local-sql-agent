# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Do the session and memory policies hold this warehouse's values?

Two required files describe one dimension of the warehouse, the one a ranking
question is scoped to when it names no scope of its own:

* ``session_policy.yaml`` (``default_scope``): which table it is
  (``filter_key``), the label shown when the question leaves it open, and the
  options a clarification offers;
* ``memory_policy.yaml`` (``keys``): the closed set of values an analyst may pin
  (``options``) and the column they constrain (``column``).

No setup tool writes them, so on a first install both are the shipped template
whose options are made-up samples: the application starts, memory "works", and
every real value an analyst tries to pin is outside the closed set and refused,
while the clarification offers values the warehouse does not have. Nothing
said so. This module reads the two files and answers, as plain data that
``scripts/verify_deployment.py`` turns into a preflight check, whether they
name a real table and column of ``schema.yaml``, whether their options are
still the template's samples, and (when the application has cached the
dimension) how many options are not in it. ``core.policy_drafts`` and
``scripts/draft_policies.py`` are what writes real values.

File reads only; the dimension cache is read through a lookup the caller passes
in. Nothing returned or raised holds an option value: findings name files, keys,
tables and columns of the configuration, and counts.

Public API
----------
MEMORY_FILE, SESSION_FILE
    The two file names.
SHIPPED_DIRECTORY
    ``project_config.example/``.
Policies, PolicySite, PolicyFindings, VocabularyGap
    The two files as read, one place in them that names the dimension, what is
    wrong with them, and an option set that differs from the cached dimension.
read_policies(directory) -> Policies
shipped_policies(directory) -> Policies | None
is_shipped_directory(directory, shipped_directory) -> bool
policy_sites(policies) -> tuple[PolicySite, ...]
policy_findings(policies, view, shipped) -> PolicyFindings
vocabulary_gaps(policies, view, lookup) -> list[VocabularyGap]
cached_dimension(policies, view, lookup) -> tuple[str, str, Sequence[str]] | None
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core import config_doctor as doctor
from core.persian import normalize_for_matching

if TYPE_CHECKING:
    from knowledge.config_loader import MemoryPolicyConfig, SessionPolicyConfig

__all__ = [
    "MEMORY_FILE",
    "SESSION_FILE",
    "SHIPPED_DIRECTORY",
    "Policies",
    "PolicyFindings",
    "PolicySite",
    "VocabularyGap",
    "cached_dimension",
    "is_shipped_directory",
    "policy_findings",
    "policy_sites",
    "read_policies",
    "shipped_policies",
    "vocabulary_gaps",
]

MEMORY_FILE = "memory_policy.yaml"
SESSION_FILE = "session_policy.yaml"

#: The committed template directory, ``project_config.example/``.
SHIPPED_DIRECTORY = Path(__file__).resolve().parent.parent / "project_config.example"

#: ``lookup(table key, column) -> cached values``, ``None`` when nothing is cached.
VocabularyLookup = Callable[[str, str], "Sequence[str] | None"]


@dataclass(frozen=True)
class Policies:
    """The two policy files of one directory, parsed with the application's own validators."""

    memory_text: str
    session_text: str
    memory: MemoryPolicyConfig
    session: SessionPolicyConfig


@dataclass(frozen=True)
class PolicySite:
    """One place in the two files that names the dimension."""

    file: str
    where: str
    """``keys.<name>`` in ``memory_policy.yaml``, ``default_scope`` in ``session_policy.yaml``."""
    filter_key: str
    column: str
    """The memory key's ``column``; ``""`` for the session policy, which names none."""
    options: tuple[str, ...]
    default_label: str
    """The session policy's ``default_label``; ``""`` for a memory key."""

    @property
    def label(self) -> str:
        """``memory_policy.yaml keys.scope``: how a message names this place."""
        return f"{self.file} {self.where}"


@dataclass
class PolicyFindings:
    """What is wrong with the policies; every list names places, never values."""

    sites: tuple[PolicySite, ...] = ()
    unknown_tables: list[PolicySite] = field(default_factory=list)
    """Sites whose ``filter_key`` names no table of ``schema.yaml``."""
    unknown_columns: list[tuple[PolicySite, str]] = field(default_factory=list)
    """``(site, table key)`` where the table has no such ``column``."""
    sample_options: list[PolicySite] = field(default_factory=list)
    """Sites whose options are exactly the shipped template's sample values."""

    @property
    def problems(self) -> list[str]:
        """One sentence per finding, in file order."""
        found = [f"{site.label}: filter_key {site.filter_key!r} names no table of schema.yaml" for site in self.unknown_tables]
        found += [
            f"{site.label}: column {site.column!r} is not a column of {table} in schema.yaml"
            for site, table in self.unknown_columns
        ]
        found += [
            f"{site.label}: options are the sample values of the shipped {site.file}" for site in self.sample_options
        ]
        return found


@dataclass(frozen=True)
class VocabularyGap:
    """A site with options that are not among the values the application has cached for its dimension."""

    site: PolicySite
    table: str
    column: str
    missing: int
    """How many of the site's options (the default label aside) are not cached."""
    options: int
    cached: int
    """How many values are cached."""


def read_policies(directory: Path) -> Policies:
    """Read and validate both policy files of *directory*.

    Parameters
    ----------
    directory:
        A ``project_config``-style directory.

    Returns
    -------
    Policies

    Raises
    ------
    ValueError
        When a file cannot be read, is not YAML, or fails the validator the
        application loads it with. The message names the file and quotes
        nothing; the loader's own text, which quotes the lines it failed on, is
        the exception's ``__cause__``.

    Examples
    --------
    >>> read_policies(SHIPPED_DIRECTORY).memory.keys["scope"].column
    'Name'
    """
    from knowledge.config_loader import MemoryPolicyConfig, SessionPolicyConfig, validate_yaml_text

    texts: dict[str, str] = {}
    for name in (MEMORY_FILE, SESSION_FILE):
        try:
            texts[name] = (directory / name).read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"{name} cannot be read ({type(exc).__name__})") from exc
    parsed: dict[str, object] = {}
    for name, model in ((MEMORY_FILE, MemoryPolicyConfig), (SESSION_FILE, SessionPolicyConfig)):
        try:
            parsed[name] = validate_yaml_text(name, texts[name], model)
        except ValueError as exc:
            # The loader's text quotes the lines it failed on; leave it on __cause__.
            raise ValueError(f"{name} does not load as the application reads it") from exc
    return Policies(
        memory_text=texts[MEMORY_FILE],
        session_text=texts[SESSION_FILE],
        memory=parsed[MEMORY_FILE],  # type: ignore[arg-type]
        session=parsed[SESSION_FILE],  # type: ignore[arg-type]
    )


def shipped_policies(directory: Path = SHIPPED_DIRECTORY) -> Policies | None:
    """The template's policies, or ``None`` when the template directory is absent or unreadable."""
    try:
        return read_policies(directory)
    except ValueError:
        return None


def is_shipped_directory(directory: Path, shipped_directory: Path = SHIPPED_DIRECTORY) -> bool:
    """Whether *directory* is the template directory itself (a copy of it is not).

    The template's policies are the samples by definition, so the check that
    compares a deployment with them must not flag the template when it is what
    ``PROJECT_CONFIG_DIR`` points at (CI, a fresh clone).

    Examples
    --------
    >>> is_shipped_directory(SHIPPED_DIRECTORY), is_shipped_directory(SHIPPED_DIRECTORY.parent / "project_config")
    (True, False)
    """
    try:
        return directory.resolve() == shipped_directory.resolve()
    except OSError:
        return False


def policy_sites(policies: Policies) -> tuple[PolicySite, ...]:
    """Every place that names the dimension: ``default_scope`` first, then each memory key.

    Examples
    --------
    >>> [site.label for site in policy_sites(read_policies(SHIPPED_DIRECTORY))]
    ['session_policy.yaml default_scope', 'memory_policy.yaml keys.scope']
    """
    scope = policies.session.default_scope
    sites = [PolicySite(
        file=SESSION_FILE, where="default_scope", filter_key=scope.filter_key, column="",
        options=tuple(scope.options), default_label=scope.default_label,
    )]
    for name, entry in policies.memory.keys.items():
        sites.append(PolicySite(
            file=MEMORY_FILE, where=f"keys.{name}", filter_key=entry.filter_key, column=entry.column,
            options=tuple(entry.options), default_label="",
        ))
    return tuple(sites)


def policy_findings(
    policies: Policies,
    view: doctor.SchemaView,
    shipped: Policies | None = None,
) -> PolicyFindings:
    """What is wrong with *policies* against ``schema.yaml`` and the shipped template.

    A ``filter_key`` must name a table of *view* (the resolution the application
    uses for a configured table name: a key, or a bare name that is unambiguous
    enough to find one). A memory key's ``column`` must be a column of that
    table; a table that lists no column gives no verdict. Options are flagged
    only when they are, as a set, exactly the options of the template's
    same-file entry: a deployment that changed even one value is not flagged.

    Parameters
    ----------
    policies:
        From :func:`read_policies`.
    view:
        ``schema.yaml`` (it must have parsed).
    shipped:
        The template's policies, or ``None`` to skip the comparison (the
        template itself, or no template at hand).

    Returns
    -------
    PolicyFindings

    Examples
    --------
    >>> template = read_policies(SHIPPED_DIRECTORY)
    >>> view = doctor.read_schema(SHIPPED_DIRECTORY)
    >>> policy_findings(template, view).problems
    []
    >>> [p.split(":")[0] for p in policy_findings(template, view, shipped=template).problems]
    ['session_policy.yaml default_scope', 'memory_policy.yaml keys.scope']
    """
    findings = PolicyFindings(sites=policy_sites(policies))
    by_key = {table.key: table for table in view.tables}
    template_options: dict[str, list[frozenset[str]]] = {}
    for site in policy_sites(shipped) if shipped is not None else ():
        if site.options:
            template_options.setdefault(site.file, []).append(frozenset(site.options))
    for site in findings.sites:
        keys = doctor.resolve_table_keys(view, site.filter_key)
        if not keys:
            findings.unknown_tables.append(site)
        elif site.column:
            tables = [by_key[key] for key in keys]
            listed = [table for table in tables if table.columns]
            if listed and not any(doctor.column_of(table, site.column) for table in listed):
                findings.unknown_columns.append((site, keys[0]))
        if site.options and frozenset(site.options) in template_options.get(site.file, ()):
            findings.sample_options.append(site)
    return findings


def vocabulary_gaps(
    policies: Policies,
    view: doctor.SchemaView,
    lookup: VocabularyLookup,
) -> list[VocabularyGap]:
    """The option sets that hold values the application's cached dimension does not.

    The application caches the distinct values of a table's
    ``prefetchable_columns``. When the dimension the policies are written for is
    cached, an option that is not among its values (compared as the application
    compares names: Persian and Arabic letter forms, digits and spacing folded)
    cannot match anything in the warehouse. The session policy's
    ``default_label`` is not a warehouse value and is left out of the count; the
    session policy names no column, so it is read against the column of a memory
    key written for the same table.

    Parameters
    ----------
    policies:
        From :func:`read_policies`.
    view:
        ``schema.yaml`` (it must have parsed).
    lookup:
        ``(table key, column) -> cached values``; ``None`` when nothing is cached.
        It must not query anything.

    Returns
    -------
    list[VocabularyGap]
        Only the sites with at least one option that is not cached.

    Examples
    --------
    >>> policies = read_policies(SHIPPED_DIRECTORY)
    >>> view = doctor.read_schema(SHIPPED_DIRECTORY)
    >>> [(g.site.label, g.missing, g.options, g.cached) for g in
    ...  vocabulary_gaps(policies, view, lambda table, column: ["Ring A", "Ring B"])]
    []
    >>> [(g.site.label, g.missing, g.options, g.cached) for g in
    ...  vocabulary_gaps(policies, view, lambda table, column: ["Ring A", "Ring C"])]
    [('session_policy.yaml default_scope', 1, 2, 2), ('memory_policy.yaml keys.scope', 1, 2, 2)]
    >>> vocabulary_gaps(policies, view, lambda table, column: None)
    []
    """
    by_key = {table.key: table for table in view.tables}
    sites = policy_sites(policies)
    column_for_table: dict[str, str] = {}
    for site in sites:
        if site.column:
            for key in doctor.resolve_table_keys(view, site.filter_key):
                column_for_table.setdefault(key, site.column)
    gaps: list[VocabularyGap] = []
    for site in sites:
        for key in doctor.resolve_table_keys(view, site.filter_key):
            wanted = site.column or column_for_table.get(key, "")
            column = doctor.column_of(by_key[key], wanted) if wanted else ""
            cached = lookup(key, column) if column else None
            if cached is None:
                continue
            known = {normalize_for_matching(value) for value in cached}
            default = normalize_for_matching(site.default_label) if site.default_label else None
            options = [o for o in site.options if normalize_for_matching(o) != default]
            missing = sum(1 for option in options if normalize_for_matching(option) not in known)
            if missing:
                gaps.append(VocabularyGap(site, key, column, missing, len(options), len(known)))
            break
    return gaps


def cached_dimension(
    policies: Policies,
    view: doctor.SchemaView,
    lookup: VocabularyLookup,
) -> tuple[str, str, Sequence[str]] | None:
    """The table, column and cached values of the dimension the policies are written for.

    The same resolution as :func:`vocabulary_gaps` (the first place that names a
    table of *view* whose column the application has cached), for a caller that
    compares something other than the policies' options with the values: the
    keys of ``aliases.yaml`` ``ring_aliases``.

    Parameters
    ----------
    policies:
        From :func:`read_policies`.
    view:
        ``schema.yaml`` (it must have parsed).
    lookup:
        ``(table key, column) -> cached values``; ``None`` when nothing is cached.
        It must not query anything.

    Returns
    -------
    tuple[str, str, Sequence[str]] | None
        ``(table key, column, values)``; ``None`` when no dimension is cached.

    Examples
    --------
    >>> policies = read_policies(SHIPPED_DIRECTORY)
    >>> view = doctor.read_schema(SHIPPED_DIRECTORY)
    >>> cached_dimension(policies, view, lambda table, column: ["Ring A"])
    ('Ring', 'Name', ['Ring A'])
    >>> cached_dimension(policies, view, lambda table, column: None) is None
    True
    """
    by_key = {table.key: table for table in view.tables}
    sites = policy_sites(policies)
    column_for_table: dict[str, str] = {}
    for site in sites:
        if site.column:
            for key in doctor.resolve_table_keys(view, site.filter_key):
                column_for_table.setdefault(key, site.column)
    for site in sites:
        for key in doctor.resolve_table_keys(view, site.filter_key):
            wanted = site.column or column_for_table.get(key, "")
            column = doctor.column_of(by_key[key], wanted) if wanted else ""
            cached = lookup(key, column) if column else None
            if cached is not None:
                return key, column, cached
            break
    return None
