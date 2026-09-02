# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for retrieval/value_resolver.py (resolve_value) -- Phase 5b.

No live database anywhere in this file: every test injects ``execute_fn``,
exactly the way ``SQLAgent(execute_fn=...)`` is injected throughout the rest
of this suite.

Per the phase spec, the injection test (:class:`TestResolveValueInjection`)
is written and observed failing *before* ``retrieval/value_resolver.py``
exists at all -- it is the test that drives the module's design, not an
afterthought.
"""

from __future__ import annotations

import pandas as pd
import pytest

from retrieval.value_resolver import clear_resolution_cache, resolve_value
from security.auth import Principal


@pytest.fixture(autouse=True)
def _clean_resolution_cache():
    """Every test gets an empty resolution cache.

    Without this, two parametrized cases (or two tests) that happen to
    resolve the same mention/table/column/scope would share a cache entry
    and the second one's ``execute_fn`` would never be called -- exactly
    the behaviour :class:`TestResolveValueCache` exists to test
    *deliberately*, but a false positive/negative everywhere else.
    """
    clear_resolution_cache()
    yield
    clear_resolution_cache()


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _RecordingExecutor:
    """Fake ``execute_fn`` that records every (sql, params) call it receives.

    Returns a canned single-row DataFrame by default (one match) so a test
    that doesn't care about the *value* returned doesn't have to build one.
    """

    def __init__(self, frame: pd.DataFrame | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._frame = frame

    def __call__(self, sql: str, params) -> pd.DataFrame:
        self.calls.append((sql, tuple(params)))
        if self._frame is not None:
            return self._frame
        return pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})


# ---------------------------------------------------------------------------
# 4. Injection -- the test that matters most; written first.
# ---------------------------------------------------------------------------


class TestResolveValueInjection:
    """Parametrised over hostile mentions: the SQL text must never move.

    Only the bound parameter varies. If any of these inputs changes the
    *shape* of the generated SQL string, the design is wrong.
    """

    _HOSTILE_MENTIONS = [
        "O'Brien",
        "a' OR '1'='1",
        "value'; DROP TABLE Customer; --",
        "--comment",
        "; DROP TABLE Customer;",
        "50%",
        "under_score",
        "]Customer",
        "علی' یا '1'='1",  # Persian mention with an embedded quote
    ]

    @pytest.mark.parametrize("mention", _HOSTILE_MENTIONS)
    def test_sql_text_is_byte_identical_across_hostile_mentions(self, mention):
        executor = _RecordingExecutor()
        resolve_value("baseline mention", ["Customer"], execute_fn=executor)
        baseline_sql = executor.calls[0][0]

        executor2 = _RecordingExecutor()
        resolve_value(mention, ["Customer"], execute_fn=executor2)
        hostile_sql = executor2.calls[0][0]

        assert hostile_sql == baseline_sql, (
            f"SQL text changed for mention {mention!r}: "
            f"{baseline_sql!r} != {hostile_sql!r}"
        )
        # The mention must never appear inside the SQL text itself -- it may
        # only ever travel as a bound parameter value.
        assert mention not in hostile_sql

    @pytest.mark.parametrize("mention", _HOSTILE_MENTIONS)
    def test_mention_only_ever_appears_as_a_bound_parameter(self, mention):
        from retrieval.value_resolver import _escape_like_wildcards

        executor = _RecordingExecutor()
        resolve_value(mention, ["Customer"], execute_fn=executor)

        assert len(executor.calls) == 1
        sql, params = executor.calls[0]
        assert mention not in sql
        # The bound LIKE value is the mention wrapped in wildcards, with
        # any literal %/_/[ in the mention itself escaped (see
        # TestLikeWildcardEscaping) -- never the raw mention interpolated
        # into the SQL text.
        assert params[1] == f"%{_escape_like_wildcards(mention)}%"

    def test_generated_sql_shape(self):
        """Documents the exact fixed template -- ``TOP (?)`` parenthesised
        form plus the ``ESCAPE`` clause that makes a literal %, _, or [ in
        the mention match literally rather than act as a wildcard."""
        executor = _RecordingExecutor()
        resolve_value("مبارکه", ["Customer"], execute_fn=executor)
        sql, params = executor.calls[0]
        assert sql == (
            "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Customer] "
            "WHERE [Name] LIKE ? ESCAPE '\\'"
        )
        assert params[1] == "%مبارکه%"


# ---------------------------------------------------------------------------
# 1-3. The three match outcomes
# ---------------------------------------------------------------------------


class TestResolveValueOutcomes:
    def test_single_match_reaches_filters(self):
        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        result = resolve_value("فولاد مبارکه", ["Customer"], execute_fn=executor)
        assert result.status == "matched"
        assert result.filters == {"Customer": "شرکت فولاد مبارکه اصفهان"}
        assert result.clarification is None

    def test_several_matches_surface_as_ambiguity_not_silently_chosen(self):
        frame = pd.DataFrame({"Name": ["شرکت الف", "شرکت ب"]})
        executor = _RecordingExecutor(frame=frame)
        result = resolve_value("شرکت", ["Customer"], execute_fn=executor)
        assert result.status == "ambiguous"
        assert result.filters == {}
        assert result.clarification is not None
        assert set(result.clarification.options) == {"شرکت الف", "شرکت ب"}

    def test_no_match_returns_empty_without_raising(self):
        frame = pd.DataFrame({"Name": []})
        executor = _RecordingExecutor(frame=frame)
        result = resolve_value("چیز ناشناخته", ["Customer"], execute_fn=executor)
        assert result.status == "no_match"
        assert result.filters == {}
        assert result.clarification is None


# ---------------------------------------------------------------------------
# 5. Allowlist refusal
# ---------------------------------------------------------------------------


class TestResolveValueAllowlist:
    def test_target_outside_allowlist_is_refused_without_building_sql(self):
        executor = _RecordingExecutor()
        result = resolve_value("anything", ["HR_Payroll"], execute_fn=executor)
        assert result.status == "no_match"
        assert result.miss_reason == "not_in_allowlist"
        assert executor.calls == []

    def test_unknown_table_mixed_with_known_table_still_queries_known_one(self):
        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        result = resolve_value("مبارکه", ["HR_Payroll", "Customer"], execute_fn=executor)
        assert result.status == "matched"
        assert len(executor.calls) == 1


# ---------------------------------------------------------------------------
# 6. ACL refusal
# ---------------------------------------------------------------------------


class TestResolveValueACL:
    def test_denied_column_gets_no_resolution_and_no_values(self):
        executor = _RecordingExecutor()
        principal = Principal(id="p1", name="P1", denied_columns=("Name",))
        result = resolve_value(
            "فولاد مبارکه", ["Customer"], principal=principal, execute_fn=executor
        )
        assert result.status == "no_match"
        assert result.miss_reason == "denied_by_acl"
        assert result.filters == {}
        assert executor.calls == []

    def test_denial_is_column_specific_not_table_wide(self):
        # Symbol has two allowlisted columns; denying one still allows the other.
        frame = pd.DataFrame({"Commodity_Symbol": ["GOLD"]})
        executor = _RecordingExecutor(frame=frame)
        principal = Principal(
            id="p1", name="P1", denied_columns=("Commodity_PersianName",)
        )
        result = resolve_value(
            "GOLD", ["Symbol"], principal=principal, execute_fn=executor
        )
        assert result.status == "matched"
        assert all("Commodity_PersianName" not in sql for sql, _ in executor.calls)


# ---------------------------------------------------------------------------
# 7. Cache
# ---------------------------------------------------------------------------


class TestResolveValueCache:
    def test_second_identical_resolution_issues_no_second_query(self):
        from retrieval.value_resolver import clear_resolution_cache

        clear_resolution_cache()
        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        principal = Principal(id="p1", name="P1")

        r1 = resolve_value("مبارکه", ["Customer"], principal=principal, execute_fn=executor)
        r2 = resolve_value("مبارکه", ["Customer"], principal=principal, execute_fn=executor)

        assert r1 == r2
        assert len(executor.calls) == 1

    def test_different_scope_keys_do_not_share_a_cache_entry(self):
        from retrieval.value_resolver import clear_resolution_cache

        clear_resolution_cache()
        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        p1 = Principal(id="p1", name="P1", denied_columns=("X",))
        p2 = Principal(id="p2", name="P2", denied_columns=("Y",))

        resolve_value("مبارکه", ["Customer"], principal=p1, execute_fn=executor)
        resolve_value("مبارکه", ["Customer"], principal=p2, execute_fn=executor)

        assert len(executor.calls) == 2

    def test_re_setting_a_still_live_key_touches_it_for_lru_without_erroring(self):
        """Internal cache detail: writing a key that is already present
        (and not expired) updates it in place and moves it to the
        most-recently-used end, rather than growing the store or evicting
        an unrelated entry."""
        from retrieval.value_resolver import _cache

        key = ("مبارکه", "Customer", "Name", "scope")
        _cache.set(key, ["اول"])
        _cache.set(key, ["دوم"])
        assert _cache.get(key) == ["دوم"]


# ---------------------------------------------------------------------------
# 8. Timeout
# ---------------------------------------------------------------------------


class TestResolveValueTimeout:
    def test_timeout_falls_back_to_no_match_instead_of_raising(self):
        import time as _time

        from retrieval.value_resolver import clear_resolution_cache
        import config as cfg

        clear_resolution_cache()

        # Deliberately short (not the 2s+ this test originally used): the
        # "slow" execute_fn keeps running in the shared background thread
        # pool for a while *after* resolve_value has already given up and
        # returned -- see _POOL's docstring. A multi-second orphaned sleep
        # here would keep running well into unrelated, later tests in the
        # same process and was the actual source of a rare, hard-to-explain
        # full-suite flake (an unrelated test failing only when run as part
        # of the whole session, never in isolation). Keeping a healthy
        # margin (10x) over the timeout while shrinking the absolute
        # wall-clock duration removes that window almost entirely without
        # making the timeout itself unreliable on a loaded machine.
        def slow_execute(sql, params):
            _time.sleep(0.2)
            return pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})

        with cfg.override_settings(resolve_value_timeout_seconds=0.02):
            result = resolve_value("مبارکه", ["Customer"], execute_fn=slow_execute)

        assert result.status == "no_match"
        assert result.miss_reason == "timeout"

    def test_execute_fn_raising_falls_back_to_no_match(self):
        from retrieval.value_resolver import clear_resolution_cache

        clear_resolution_cache()

        def failing_execute(sql, params):
            raise RuntimeError("Database error: simulated failure")

        result = resolve_value("مبارکه", ["Customer"], execute_fn=failing_execute)
        assert result.status == "no_match"
        assert result.miss_reason == "error"


# ---------------------------------------------------------------------------
# Phase 7 seam: exported tool definition, not wired to any agentic loop.
# ---------------------------------------------------------------------------


class TestToolDefinitionExport:
    def test_tool_definition_is_exported_with_name_description_schema(self):
        from retrieval.value_resolver import RESOLVE_VALUE_TOOL_DEFINITION

        assert RESOLVE_VALUE_TOOL_DEFINITION["name"] == "resolve_value"
        assert isinstance(RESOLVE_VALUE_TOOL_DEFINITION["description"], str)
        assert RESOLVE_VALUE_TOOL_DEFINITION["description"]

        params = RESOLVE_VALUE_TOOL_DEFINITION["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) == {"mention", "candidate_tables"}
        assert "mention" in params["properties"]
        assert "candidate_tables" in params["properties"]

    def test_tool_definition_table_enum_matches_the_allowlist(self):
        from retrieval.value_resolver import RESOLVABLE_COLUMNS, RESOLVE_VALUE_TOOL_DEFINITION

        enum = RESOLVE_VALUE_TOOL_DEFINITION["parameters"]["properties"][
            "candidate_tables"
        ]["items"]["enum"]
        assert set(enum) == set(RESOLVABLE_COLUMNS)


# ---------------------------------------------------------------------------
# Cache internals: TTL expiry, disabled cache, LRU eviction.
# ---------------------------------------------------------------------------


class TestResolveValueCacheInternals:
    def test_ttl_zero_disables_the_cache(self):
        import config as cfg

        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        with cfg.override_settings(resolve_value_cache_ttl_seconds=0):
            resolve_value("مبارکه", ["Customer"], execute_fn=executor)
            resolve_value("مبارکه", ["Customer"], execute_fn=executor)
        assert len(executor.calls) == 2

    def test_expired_entry_is_re_queried(self):
        import config as cfg
        from retrieval import value_resolver

        frame = pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
        executor = _RecordingExecutor(frame=frame)
        with cfg.override_settings(resolve_value_cache_ttl_seconds=300):
            resolve_value("مبارکه", ["Customer"], execute_fn=executor)
            assert len(executor.calls) == 1
            # Force every cached entry to look already-expired without
            # sleeping in a test.
            for key in list(value_resolver._cache._store):
                values, _ = value_resolver._cache._store[key]
                value_resolver._cache._store[key] = (values, 0.0)
            resolve_value("مبارکه", ["Customer"], execute_fn=executor)
        assert len(executor.calls) == 2

    def test_lru_eviction_respects_max_size(self):
        import config as cfg

        frame = pd.DataFrame({"Name": ["x"]})
        executor = _RecordingExecutor(frame=frame)
        with cfg.override_settings(resolve_value_cache_ttl_seconds=300, resolve_value_cache_max_size=1):
            resolve_value("اول", ["Customer"], execute_fn=executor)
            resolve_value("دوم", ["Customer"], execute_fn=executor)
            # The "اول" entry should have been evicted -- re-resolving it
            # issues a fresh query rather than a cache hit.
            resolve_value("اول", ["Customer"], execute_fn=executor)
        assert len(executor.calls) == 3


# ---------------------------------------------------------------------------
# Coordinator finding #2: LIKE metacharacters were not escaped. Not an
# injection (the value stays bound either way) but a correctness bug: an
# un-escaped "%", "_", or "[" in the mention changes which rows match.
# ---------------------------------------------------------------------------


class TestLikeWildcardEscaping:
    def test_percent_in_mention_does_not_become_a_wildcard(self):
        from retrieval.value_resolver import _escape_like_wildcards

        assert _escape_like_wildcards("50%") == "50\\%"

    def test_underscore_in_mention_does_not_match_any_single_char(self):
        from retrieval.value_resolver import _escape_like_wildcards

        assert _escape_like_wildcards("under_score") == "under\\_score"

    def test_bracket_in_mention_does_not_open_a_character_class(self):
        from retrieval.value_resolver import _escape_like_wildcards

        assert _escape_like_wildcards("[a-z]") == "\\[a-z]"

    def test_closing_bracket_alone_is_not_escaped(self):
        """']' is only special to T-SQL LIKE inside a '[...]' class -- a
        lone ']' is literal and needs no escaping."""
        from retrieval.value_resolver import _escape_like_wildcards

        assert _escape_like_wildcards("]Customer") == "]Customer"

    def test_a_bare_percent_mention_is_bound_escaped_not_a_match_all(self):
        """Before the fix, mention='%' produced the LIKE pattern '%%%',
        which matches every row in the table up to the TOP cap. After the
        fix it must be bound as the literal-percent pattern '%\\%%'."""
        executor = _RecordingExecutor(frame=pd.DataFrame({"Name": []}))
        resolve_value("%", ["Customer"], execute_fn=executor)
        sql, params = executor.calls[0]
        assert params[1] == "%\\%%"

    def test_mixed_wildcards_all_escaped_together(self):
        from retrieval.value_resolver import _escape_like_wildcards

        assert _escape_like_wildcards("فول%اد_[x]") == "فول\\%اد\\_\\[x]"
