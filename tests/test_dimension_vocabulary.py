# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for retrieval/dimension_vocabulary.py -- Phase 5b redesign.

No live database anywhere in this file: ``refresh_vocabulary``/``warm_all``
always take an injected ``execute_fn``, and ``match_question_against_vocabulary``
never issues a query *synchronously* -- that is the property this module
exists to guarantee. The background-refresh trigger it fires on a
stale/absent entry is disabled process-wide by ``tests/conftest.py``'s
autouse ``_no_background_dimension_refresh`` fixture, exactly like every
other test in this suite; the tests in ``TestBackgroundRefresh`` below are
the only ones that turn it back on, always with an injected ``execute_fn``,
always inside a ``try/finally`` that turns it back off before the test ends.
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from core.persian import normalize_for_matching
from retrieval.dimension_vocabulary import (
    MIN_MATCH_LENGTH,
    PREFETCH_COLUMNS,
    clear_vocabulary_cache,
    get_cached_vocabulary,
    get_vocabulary_status,
    manual_refresh,
    match_question_against_vocabulary,
    refresh_vocabulary,
    set_background_refresh_enabled,
    warm_all,
)
from security.auth import Principal


@pytest.fixture(autouse=True)
def _clean_vocabulary_cache():
    clear_vocabulary_cache()
    yield
    clear_vocabulary_cache()


def _fake_execute(values, column="Name"):
    def execute_fn(sql, params):
        return pd.DataFrame({column: values})
    return execute_fn


# ---------------------------------------------------------------------------
# The allowlist deliberately excludes Customer/Supplier.
# ---------------------------------------------------------------------------


class TestPrefetchAllowlist:
    def test_customer_and_supplier_are_not_prefetch_eligible(self):
        assert "Customer" not in PREFETCH_COLUMNS
        assert "Supplier" not in PREFETCH_COLUMNS

    def test_small_dimensions_are_prefetch_eligible(self):
        assert set(PREFETCH_COLUMNS) == {
            "Broker", "Currency", "DeliveryPlace", "Ring", "Symbol",
        }


# ---------------------------------------------------------------------------
# refresh_vocabulary / warm_all / get_cached_vocabulary
# ---------------------------------------------------------------------------


class TestRefreshAndCachedRead:
    def test_refresh_populates_the_cache(self):
        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        assert get_cached_vocabulary("Ring", "Name") == ["تالار پتروشیمی"]

    def test_uncached_read_returns_none(self):
        # No refresh_vocabulary call happened -- the cache is cold.
        assert get_cached_vocabulary("Ring", "Name") is None

    def test_warm_all_refreshes_every_prefetch_pair(self):
        counts = warm_all(execute_fn=_fake_execute(["x", "y", "z"]))
        expected_keys = {
            f"{table}.{column}"
            for table, columns in PREFETCH_COLUMNS.items()
            for column in columns
        }
        assert set(counts) == expected_keys
        assert all(v == 3 for v in counts.values())
        for key in expected_keys:
            table, column = key.split(".")
            assert get_cached_vocabulary(table, column) == ["x", "y", "z"]

    def test_warm_all_survives_one_failing_column(self):
        calls = {"n": 0}

        def flaky_execute(sql, params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated DB failure")
            return pd.DataFrame({"Name": ["ok"]})

        counts = warm_all(execute_fn=flaky_execute)
        total_pairs = sum(len(cols) for cols in PREFETCH_COLUMNS.values())
        # One pair failed and is absent from counts; the rest still warmed.
        assert len(counts) == total_pairs - 1

    def test_prefetch_query_has_no_where_clause_or_user_input(self):
        from retrieval.dimension_vocabulary import _prefetch_query

        sql = _prefetch_query("Ring", "Name")
        assert "WHERE" not in sql
        # The schema qualifier itself comes from schema.yaml (see
        # retrieval.dimension_vocabulary._TABLE_SCHEMAS) and legitimately
        # differs between the real config and project_config.example/ --
        # this checks the fixed shape around it, not the literal qualifier.
        assert sql.startswith("SELECT DISTINCT TOP (?) [Name] FROM [")
        assert sql.endswith("].[Ring]")


# ---------------------------------------------------------------------------
# match_question_against_vocabulary never blocks on a query.
# ---------------------------------------------------------------------------


class TestMatchNeverBlocks:
    def test_cold_cache_returns_no_match_synchronously(self):
        # No refresh_vocabulary call at all -- every table is a cold miss.
        # (Background refresh is disabled suite-wide by conftest.py.)
        result = match_question_against_vocabulary(
            normalize_for_matching("تالار پتروشیمی کجاست"), ["Ring"],
        )
        assert result.filters == {}
        assert result.clarifications == []
        assert result.resolved_columns == ()

    def test_table_outside_the_prefetch_allowlist_is_silently_skipped(self):
        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        result = match_question_against_vocabulary(
            normalize_for_matching("مشتری فولاد مبارکه"), ["Customer"],
        )
        assert result.filters == {}
        assert result.resolved_columns == ()

    def test_background_refresh_is_disabled_by_default_in_this_suite(self):
        """Sanity check on the conftest wiring itself -- this file relies
        on it for every other test, so it must actually be in effect."""
        from retrieval.dimension_vocabulary import is_background_refresh_enabled

        assert is_background_refresh_enabled() is False


# ---------------------------------------------------------------------------
# Matching rules: longest match wins, ties are ambiguous, min length,
# several dimensions in one question.
# ---------------------------------------------------------------------------


class TestMatchingRules:
    def test_longest_match_wins_over_a_substring_also_present(self):
        refresh_vocabulary(
            "Symbol", "Commodity_PersianName",
            execute_fn=_fake_execute(["فولاد", "فولاد مبارکه"], column="Commodity_PersianName"),
        )
        refresh_vocabulary(
            "Symbol", "Commodity_Symbol", execute_fn=_fake_execute([], column="Commodity_Symbol"),
        )
        result = match_question_against_vocabulary(
            normalize_for_matching("قیمت فولاد مبارکه چند بود"), ["Symbol"],
        )
        assert result.filters == {"Symbol": "فولاد مبارکه"}

    def test_tied_longest_matches_are_ambiguous_not_silently_picked(self):
        refresh_vocabulary(
            "Currency", "PersianName",
            execute_fn=_fake_execute(["دلار آمریکا", "دلار کانادا"], column="PersianName"),
        )
        result = match_question_against_vocabulary(
            normalize_for_matching("نرخ دلار آمریکا و دلار کانادا"), ["Currency"],
        )
        assert result.filters == {}
        assert len(result.clarifications) == 1
        assert set(result.clarifications[0].options) == {"دلار آمریکا", "دلار کانادا"}
        assert result.clarifications[0].field == "Currency"

    def test_values_shorter_than_min_match_length_are_never_matched(self):
        short_value = "ط"  # 1 char, well under MIN_MATCH_LENGTH
        assert len(short_value) < MIN_MATCH_LENGTH
        refresh_vocabulary(
            "Symbol", "Commodity_Symbol",
            execute_fn=_fake_execute([short_value], column="Commodity_Symbol"),
        )
        refresh_vocabulary(
            "Symbol", "Commodity_PersianName",
            execute_fn=_fake_execute([], column="Commodity_PersianName"),
        )
        result = match_question_against_vocabulary(
            normalize_for_matching(f"{short_value} چیست"), ["Symbol"],
        )
        assert result.filters == {}

    def test_multiple_dimensions_in_one_question_all_resolve(self):
        refresh_vocabulary(
            "Ring", "Name",
            execute_fn=_fake_execute(["تالار محصولات صنعتی", "تالار پتروشیمی"]),
        )
        refresh_vocabulary(
            "Symbol", "Commodity_PersianName",
            execute_fn=_fake_execute(["فولاد مبارکه"], column="Commodity_PersianName"),
        )
        refresh_vocabulary(
            "Symbol", "Commodity_Symbol", execute_fn=_fake_execute([], column="Commodity_Symbol"),
        )
        q = normalize_for_matching(
            "گرانترین معامله فولاد مبارکه در تالار محصولات صنعتی چقدر بود"
        )
        result = match_question_against_vocabulary(q, ["Ring", "Symbol"])
        assert result.filters == {
            "Ring": "تالار محصولات صنعتی",
            "Symbol": "فولاد مبارکه",
        }
        assert set(result.resolved_columns) == {
            "Ring.Name", "Symbol.Commodity_PersianName", "Symbol.Commodity_Symbol",
        }


# ---------------------------------------------------------------------------
# ACL -- a denied column excludes its vocabulary from the match, same as
# the forward path (resolve_value).
# ---------------------------------------------------------------------------


class TestACL:
    def test_denied_column_excludes_that_columns_vocabulary(self):
        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        principal = Principal(id="p1", name="P1", denied_columns=("Name",))
        result = match_question_against_vocabulary(
            normalize_for_matching("تالار پتروشیمی"), ["Ring"], principal=principal,
        )
        assert result.filters == {}
        assert result.resolved_columns == ()

    def test_denial_is_column_specific_within_a_multi_column_table(self):
        refresh_vocabulary(
            "Symbol", "Commodity_PersianName",
            execute_fn=_fake_execute(["فولاد مبارکه"], column="Commodity_PersianName"),
        )
        refresh_vocabulary(
            "Symbol", "Commodity_Symbol",
            execute_fn=_fake_execute(["FOOLAD"], column="Commodity_Symbol"),
        )
        principal = Principal(id="p1", name="P1", denied_columns=("Commodity_PersianName",))
        result = match_question_against_vocabulary(
            normalize_for_matching("نماد FOOLAD"), ["Symbol"], principal=principal,
        )
        assert result.filters == {"Symbol": "FOOLAD"}
        assert result.resolved_columns == ("Symbol.Commodity_Symbol",)

    def test_cache_is_not_principal_scoped_only_the_match_pool_is_filtered(self):
        """Two principals share the same cached vocabulary -- the ACL gate
        is applied every time a question is matched, not baked into a
        per-principal cache partition."""
        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        denied = Principal(id="p1", name="P1", denied_columns=("Name",))
        allowed = Principal(id="p2", name="P2")

        r1 = match_question_against_vocabulary(
            normalize_for_matching("تالار پتروشیمی"), ["Ring"], principal=denied,
        )
        r2 = match_question_against_vocabulary(
            normalize_for_matching("تالار پتروشیمی"), ["Ring"], principal=allowed,
        )
        assert r1.filters == {}
        assert r2.filters == {"Ring": "تالار پتروشیمی"}


# ---------------------------------------------------------------------------
# Stale-while-revalidate: a stale entry is still SERVED, not treated as a
# miss -- and (with the background trigger re-enabled locally) a refresh
# is triggered for it. Absent entries are still a synchronous miss.
# ---------------------------------------------------------------------------


def _force_stale(table: str, column: str) -> None:
    """Rewrite a cached entry's expiry to the past, without sleeping."""
    from retrieval import dimension_vocabulary

    values, _expires_at, fetched_at = dimension_vocabulary._cache._store[(table, column)]
    dimension_vocabulary._cache._store[(table, column)] = (values, 0.0, fetched_at)


class TestStaleWhileRevalidate:
    def test_stale_entry_is_still_served_not_treated_as_a_miss(self):
        import config as cfg

        with cfg.override_settings(dimension_vocabulary_ttl_seconds=300):
            refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
            _force_stale("Ring", "Name")
            # Background refresh stays disabled (conftest default) for this
            # assertion -- it isolates "is the stale value still served"
            # from "is a refresh triggered", which the next test covers.
            result = match_question_against_vocabulary(
                normalize_for_matching("تالار پتروشیمی"), ["Ring"],
            )
        assert result.filters == {"Ring": "تالار پتروشیمی"}
        assert result.resolved_columns == ("Ring.Name",)

    def test_ttl_zero_makes_every_read_stale_but_still_served(self):
        """dimension_vocabulary_ttl_seconds <= 0 is documented as "the
        closest thing to disabled" -- it no longer discards the value
        outright (that would throw away a perfectly good answer); it
        stores it pre-expired, so it is served once and then treated as
        stale on every subsequent read."""
        import config as cfg

        with cfg.override_settings(dimension_vocabulary_ttl_seconds=0):
            refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
            result = match_question_against_vocabulary(
                normalize_for_matching("تالار پتروشیمی"), ["Ring"],
            )
        assert result.filters == {"Ring": "تالار پتروشیمی"}

    def test_absent_entry_is_still_a_synchronous_miss(self):
        # Never refreshed at all -- contrast with the stale case above,
        # which DOES serve a value. Nothing to serve here.
        result = match_question_against_vocabulary(
            normalize_for_matching("تالار پتروشیمی"), ["Ring"],
        )
        assert result.filters == {}
        assert result.resolved_columns == ()


# ---------------------------------------------------------------------------
# Background refresh: single-flight, non-blocking, non-poisoning on
# failure, rate-limited on failure. These are the only tests in this file
# that turn the (suite-wide disabled) trigger back on -- always with an
# injected execute_fn, always restored afterwards.
# ---------------------------------------------------------------------------


class TestBackgroundRefresh:
    @pytest.fixture(autouse=True)
    def _enable_locally(self):
        """Re-enable the trigger for this class only, and reset its
        module-level single-flight/backoff bookkeeping on both sides of
        every test -- ``_in_flight``/``_last_failure`` are shared, global
        dicts (there is deliberately no per-test cache object to isolate
        them the way ``clear_vocabulary_cache()`` isolates the value
        cache), so a backoff timestamp left behind by one test would
        silently suppress the very first trigger of the next one testing
        the same ``("Ring", "Name")`` key."""
        import retrieval.dimension_vocabulary as dv
        from retrieval.dimension_vocabulary import is_background_refresh_enabled

        dv._in_flight.clear()
        dv._last_failure.clear()
        previous = is_background_refresh_enabled()
        set_background_refresh_enabled(True)
        try:
            yield
        finally:
            # Save/restore, not a hardcoded False -- see
            # tests/conftest.py's _no_background_dimension_refresh
            # docstring for why a hardcoded restore here is exactly what
            # caused a real background DB call once already.
            set_background_refresh_enabled(previous)
            dv._in_flight.clear()
            dv._last_failure.clear()

    def _wait_for_quiescence(self, timeout: float = 2.0) -> None:
        """Block until no background refresh is in flight, for deterministic
        assertions -- polls the module's own bookkeeping rather than
        sleeping a guessed duration."""
        from retrieval.dimension_vocabulary import _bg_lock, _in_flight

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with _bg_lock:
                if not _in_flight:
                    return
            time.sleep(0.01)
        raise AssertionError("background refresh did not quiesce in time")

    def test_stale_entry_triggers_a_background_refresh(self):
        """Full story: serves the stale value immediately AND the
        background refresh it triggers actually lands, so the NEXT read
        sees the fresh value -- not just "a refresh was attempted"."""
        import config as cfg
        import retrieval.dimension_vocabulary as dv

        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["old value"]))
        _force_stale("Ring", "Name")

        # match_question_against_vocabulary has no per-call execute_fn
        # injection point (see its docstring) -- the trigger always uses
        # the module's own default, so that is what this test patches.
        # Patched (not left as the real, conftest-mocked default) and
        # waited-on (_wait_for_quiescence) specifically so this test never
        # leaves an unmocked background call still in flight when it
        # returns -- exactly the orphaned-background-work bug class this
        # phase already found once (test_value_resolver.py's leaked
        # time.sleep). Every other test in this class either mocks the
        # default the same way or never triggers a real background call
        # at all.
        original_default = dv._default_execute_fn
        dv._default_execute_fn = _fake_execute(["new value"])
        try:
            with cfg.override_settings(dimension_vocabulary_ttl_seconds=300):
                result = match_question_against_vocabulary(
                    normalize_for_matching("old value"), ["Ring"],
                )
                # Served the STALE value immediately, synchronously.
                assert result.filters == {"Ring": "old value"}

                self._wait_for_quiescence()
        finally:
            dv._default_execute_fn = original_default

        # The background refresh landed: a later read sees the fresh value.
        assert get_cached_vocabulary("Ring", "Name") == ["new value"]

    def test_absent_entry_triggers_a_background_refresh(self):
        calls = []

        def counting_execute(sql, params):
            calls.append((sql, params))
            return pd.DataFrame({"Name": ["فولاد"]})

        # Exercise the trigger directly with an injected execute_fn --
        # match_question_against_vocabulary always uses the production
        # default internally, so this is how the trigger's own behaviour
        # (as opposed to match's decision to call it) is verified without
        # a live database. Patch the module-level default so the
        # automatically-triggered call picks it up.
        import retrieval.dimension_vocabulary as dv
        original_default = dv._default_execute_fn
        dv._default_execute_fn = counting_execute
        try:
            result = match_question_against_vocabulary(
                normalize_for_matching("چیزی"), ["Ring"],
            )
            assert result.filters == {}  # absent -> synchronous miss
            self._wait_for_quiescence()
        finally:
            dv._default_execute_fn = original_default

        assert len(calls) == 1
        assert get_cached_vocabulary("Ring", "Name") == ["فولاد"]

    def test_single_flight_one_refresh_per_key_under_concurrent_triggers(self):
        """Several concurrent callers hitting the same absent key must
        launch exactly one refresh, not one each."""
        import retrieval.dimension_vocabulary as dv

        calls = []
        call_lock = threading.Lock()
        release = threading.Event()

        def slow_execute(sql, params):
            with call_lock:
                calls.append((sql, params))
            release.wait(timeout=2.0)  # hold the single in-flight refresh open
            return pd.DataFrame({"Name": ["فولاد"]})

        original_default = dv._default_execute_fn
        dv._default_execute_fn = slow_execute
        try:
            threads = [
                threading.Thread(
                    target=dv._trigger_background_refresh, args=("Ring", "Name"),
                )
                for _ in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            release.set()
            self._wait_for_quiescence()
        finally:
            dv._default_execute_fn = original_default
            release.set()

        assert len(calls) == 1, f"expected exactly 1 refresh call, got {len(calls)}"

    def test_a_failed_refresh_does_not_poison_the_entry(self):
        """A previously-cached (stale) value must survive a failed
        background refresh untouched -- the next successful refresh (not
        this test's concern) is what should ever replace it."""
        import retrieval.dimension_vocabulary as dv

        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        _force_stale("Ring", "Name")

        def failing_execute(sql, params):
            raise RuntimeError("simulated DB outage")

        original_default = dv._default_execute_fn
        dv._default_execute_fn = failing_execute
        try:
            dv._trigger_background_refresh("Ring", "Name")
            self._wait_for_quiescence()
        finally:
            dv._default_execute_fn = original_default

        # The old value is still there, completely untouched by the failure.
        assert get_cached_vocabulary("Ring", "Name") == ["تالار پتروشیمی"]

    def test_a_failed_refresh_is_not_retried_within_the_backoff_window(self):
        import retrieval.dimension_vocabulary as dv

        calls = {"n": 0}

        def failing_execute(sql, params):
            calls["n"] += 1
            raise RuntimeError("simulated DB outage")

        original_default = dv._default_execute_fn
        original_backoff = dv._BACKGROUND_REFRESH_BACKOFF_SECONDS
        dv._default_execute_fn = failing_execute
        try:
            dv._trigger_background_refresh("Ring", "Name")
            self._wait_for_quiescence()
            assert calls["n"] == 1

            # A second trigger for the same key, immediately after the
            # first one's failure, must NOT attempt again.
            dv._trigger_background_refresh("Ring", "Name")
            self._wait_for_quiescence()
            assert calls["n"] == 1, "retried within the backoff window"
        finally:
            dv._default_execute_fn = original_default
            dv._BACKGROUND_REFRESH_BACKOFF_SECONDS = original_backoff

    def test_explicit_refresh_vocabulary_ignores_the_backoff(self):
        """The backoff only throttles the AUTOMATIC trigger -- an
        operator's own refresh_vocabulary()/warm_all() call (or the
        opt-in startup warm-up) must always attempt immediately."""
        import retrieval.dimension_vocabulary as dv

        dv._last_failure[("Ring", "Name")] = time.monotonic()  # "just failed"

        result = refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["ok"]))
        assert result == ["ok"]
        dv._last_failure.pop(("Ring", "Name"), None)

    def test_background_refresh_never_raises_into_the_caller(self):
        import retrieval.dimension_vocabulary as dv

        def failing_execute(sql, params):
            raise RuntimeError("simulated DB outage")

        original_default = dv._default_execute_fn
        dv._default_execute_fn = failing_execute
        try:
            # Must return normally -- the trigger is fire-and-forget.
            dv._trigger_background_refresh("Ring", "Name")
            self._wait_for_quiescence()
        finally:
            dv._default_execute_fn = original_default


# ---------------------------------------------------------------------------
# Admin panel phase 6, §3 -- freshness reporting and manual refresh, read
# straight off the module's OWN bookkeeping (the cache's fetched_at,
# _last_failure/_last_failure_at), never a second, parallel structure.
# ---------------------------------------------------------------------------


def _reset_failure_bookkeeping():
    import retrieval.dimension_vocabulary as dv

    dv._last_failure.clear()
    dv._last_failure_at.clear()


class TestVocabularyStatus:
    @pytest.fixture(autouse=True)
    def _clean_failure_state(self):
        _reset_failure_bookkeeping()
        yield
        _reset_failure_bookkeeping()

    def test_never_cached_column_reports_not_cached(self):
        statuses = get_vocabulary_status()
        entry = next(s for s in statuses if (s["table"], s["column"]) == ("Ring", "Name"))
        assert entry["cached"] is False
        assert entry["value_count"] is None
        assert entry["fetched_at"] is None
        assert entry["is_fresh"] is False
        assert entry["last_failure"] is False
        assert entry["last_failure_at"] is None

    def test_one_entry_per_prefetch_column(self):
        expected = {
            (table, column) for table, columns in PREFETCH_COLUMNS.items() for column in columns
        }
        statuses = get_vocabulary_status()
        assert {(s["table"], s["column"]) for s in statuses} == expected

    def test_after_refresh_reports_value_count_and_fetched_at(self):
        refresh_vocabulary("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی", "تالار دیگر"]))
        entry = next(
            s for s in get_vocabulary_status() if (s["table"], s["column"]) == ("Ring", "Name")
        )
        assert entry["cached"] is True
        assert entry["value_count"] == 2
        assert entry["fetched_at"] is not None
        assert entry["is_fresh"] is True

    def test_bookkeeping_is_not_a_copy(self):
        """Reads the SAME module-level state a failed background attempt
        writes, rather than duplicating it -- flip _last_failure_at by
        hand (exactly what a background failure does) and confirm the
        status function picks it up with nothing else touched."""
        import retrieval.dimension_vocabulary as dv

        key = ("Ring", "Name")
        dv._last_failure[key] = time.monotonic()
        dv._last_failure_at[key] = "2026-01-01T00:00:00+00:00"

        entry = next(
            s for s in get_vocabulary_status() if (s["table"], s["column"]) == ("Ring", "Name")
        )
        assert entry["last_failure"] is True
        assert entry["last_failure_at"] == "2026-01-01T00:00:00+00:00"


class TestManualRefresh:
    @pytest.fixture(autouse=True)
    def _clean_failure_state(self):
        _reset_failure_bookkeeping()
        yield
        _reset_failure_bookkeeping()

    def test_manual_refresh_actually_refreshes(self):
        result = manual_refresh("Ring", "Name", execute_fn=_fake_execute(["تالار پتروشیمی"]))
        assert result["ok"] is True
        assert result["value_count"] == 1
        assert get_cached_vocabulary("Ring", "Name") == ["تالار پتروشیمی"]

    def test_failing_manual_refresh_reports_failure_not_success(self):
        def failing_execute(sql, params):
            raise RuntimeError("simulated DB outage")

        result = manual_refresh("Ring", "Name", execute_fn=failing_execute)
        assert result["ok"] is False
        assert "simulated DB outage" in result["error"]
        # Never appears to succeed: nothing was cached by this call.
        assert get_cached_vocabulary("Ring", "Name") is None

    def test_failing_manual_refresh_updates_status_bookkeeping(self):
        def failing_execute(sql, params):
            raise RuntimeError("simulated DB outage")

        manual_refresh("Ring", "Name", execute_fn=failing_execute)
        entry = next(
            s for s in get_vocabulary_status() if (s["table"], s["column"]) == ("Ring", "Name")
        )
        assert entry["last_failure"] is True
        assert entry["last_failure_at"] is not None

    def test_manual_refresh_ignores_the_automatic_backoff(self):
        """An operator explicitly asking for a refresh must get one, even
        moments after an automatic attempt just failed and backed off."""
        import retrieval.dimension_vocabulary as dv

        dv._last_failure[("Ring", "Name")] = time.monotonic()  # "just failed"
        result = manual_refresh("Ring", "Name", execute_fn=_fake_execute(["ok"]))
        assert result["ok"] is True

    def test_success_after_failure_clears_the_failure_flag(self):
        def failing_execute(sql, params):
            raise RuntimeError("simulated DB outage")

        manual_refresh("Ring", "Name", execute_fn=failing_execute)
        manual_refresh("Ring", "Name", execute_fn=_fake_execute(["ok"]))

        entry = next(
            s for s in get_vocabulary_status() if (s["table"], s["column"]) == ("Ring", "Name")
        )
        assert entry["last_failure"] is False
        assert entry["last_failure_at"] is None
