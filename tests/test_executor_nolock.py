# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``WITH (NOLOCK)`` in ``database.executor``: applied only for a routed
source whose ``datasources.yaml`` entry sets ``nolock: true``.

Same discipline as ``tests/test_executor.py``: the engine is a mock, so the
statement the executor hands to ``exec_driver_sql`` is what is inspected.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml

import config as cfg
from database.datasources import reset_datasources_cache
from database.executor import execute_sql, execute_sql_params
from tests.test_executor import _conn, _make_engine_mock

H = " WITH (NOLOCK)"
SQL = "SELECT o.Id FROM [sales].[Order] o JOIN [ref].[Location] AS l ON l.Id = o.LocationId"
HINTED_SQL = (
    f"SELECT o.Id FROM [sales].[Order] o{H} JOIN [ref].[Location] AS l{H} ON l.Id = o.LocationId"
)
PARAM_SQL = "SELECT DISTINCT TOP (?) [Name] FROM [sales].[Customer] WHERE [Name] LIKE ?"
HINTED_PARAM_SQL = (
    f"SELECT DISTINCT TOP (?) [Name] FROM [sales].[Customer]{H} WHERE [Name] LIKE ?"
)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


@pytest.fixture
def two_sources(tmp_path):
    """A ``datasources.yaml`` with one hinted and one plain source."""
    (tmp_path / "datasources.yaml").write_text(yaml.dump({
        "default": "plain",
        "datasources": {
            "plain": {"url_env": "DB_URL_PLAIN"},
            "hinted": {"url_env": "DB_URL_HINTED", "nolock": True},
            "hinted_structured": {
                "host": "db1.example.test", "database": "SalesDW",
                "username": "nlq_reader", "password_env": "DB_PASSWORD_SALES",
                "nolock": True,
            },
        },
    }, sort_keys=False), encoding="utf-8")
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield


def _sent(engine) -> list[tuple]:
    """Every ``exec_driver_sql`` call's positional args except the session setup."""
    return [
        c.args for c in _conn(engine).exec_driver_sql.call_args_list
        if "LOCK_TIMEOUT" not in c.args[0]
    ]


def _run(sql: str, **kwargs):
    engine = _make_engine_mock([(1,)], ["Id"])
    with patch("database.executor.get_engine", return_value=engine):
        execute_sql(sql, **kwargs)
    return engine


class TestOnlyTheRoutedSourcesFlagMatters:
    def test_a_source_with_nolock_gets_the_hint(self, two_sources):
        engine = _run(SQL, datasource="hinted")
        assert _sent(engine) == [(HINTED_SQL,)]

    def test_a_structured_source_with_nolock_gets_the_hint(self, two_sources):
        engine = _run(SQL, datasource="hinted_structured")
        assert _sent(engine) == [(HINTED_SQL,)]

    def test_a_source_without_nolock_runs_the_statement_as_given(self, two_sources):
        engine = _run(SQL, datasource="plain")
        assert _sent(engine) == [(SQL,)]

    def test_a_derived_source_is_the_one_whose_flag_is_read(self, two_sources):
        with patch("database.executor.resolve_datasource", return_value="hinted"):
            assert _sent(_run(SQL)) == [(HINTED_SQL,)]
        with patch("database.executor.resolve_datasource", return_value="plain"):
            assert _sent(_run(SQL)) == [(SQL,)]

    def test_an_explicit_datasource_wins_over_the_derived_one(self, two_sources):
        with patch("database.executor.resolve_datasource", return_value="hinted") as resolve:
            engine = _run(SQL, datasource="plain")
        resolve.assert_not_called()
        assert _sent(engine) == [(SQL,)]

    def test_the_engine_used_is_the_routed_sources(self, two_sources):
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine) as get_engine:
            execute_sql(SQL, datasource="hinted")
        get_engine.assert_called_once_with("hinted")

    def test_routing_sees_the_statement_as_the_caller_wrote_it(self, two_sources):
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine), \
             patch("database.executor.resolve_datasource", return_value="hinted") as resolve:
            execute_sql(SQL)
        assert resolve.call_args.args[0] == SQL

    def test_without_a_datasources_file_nothing_is_hinted(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path)):
            engine = _run(SQL)
        assert _sent(engine) == [(SQL,)]


class TestParameterisedStatements:
    def test_the_hint_is_added_and_the_params_pass_through_untouched(self, two_sources):
        engine = _make_engine_mock([("x",)], ["Name"])
        params = (10, "%مبارکه%")
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql_params(PARAM_SQL, params, datasource="hinted")
        assert _sent(engine) == [(HINTED_PARAM_SQL, params)]
        assert _sent(engine)[0][1] is params

    def test_a_plain_source_is_left_alone(self, two_sources):
        engine = _make_engine_mock([("x",)], ["Name"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql_params(PARAM_SQL, (10, "%a%"), datasource="plain")
        assert _sent(engine) == [(PARAM_SQL, (10, "%a%"))]

    def test_routing_works_the_same_way(self, two_sources):
        engine = _make_engine_mock([], ["Name"])
        with patch("database.executor.get_engine", return_value=engine), \
             patch("database.executor.resolve_datasource", return_value="hinted"):
            execute_sql_params(PARAM_SQL, (1, "%a%"))
        assert _sent(engine) == [(HINTED_PARAM_SQL, (1, "%a%"))]


class TestEverythingElseInExecuteIsUnchanged:
    def test_the_transaction_is_still_rolled_back_and_never_committed(self, two_sources):
        engine = _run(SQL, datasource="hinted")
        transaction = _conn(engine).begin.return_value
        transaction.rollback.assert_called_once()
        transaction.commit.assert_not_called()

    def test_the_lock_timeout_still_runs_before_the_hinted_query(self, two_sources):
        engine = _run(SQL, datasource="hinted")
        calls = [c.args[0] for c in _conn(engine).exec_driver_sql.call_args_list]
        lock = next(i for i, c in enumerate(calls) if "LOCK_TIMEOUT" in c)
        assert lock < calls.index(HINTED_SQL)

    def test_results_are_returned_as_before(self, two_sources):
        engine = _make_engine_mock([(7,), (8,)], ["Id"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql(SQL, datasource="hinted")
        assert list(df["Id"]) == [7, 8]

    def test_stream_results_and_the_row_cap_still_apply(self, two_sources):
        engine = _make_engine_mock([(1,)], ["Id"])
        with cfg.override_settings(max_rows_returned=5), \
             patch("database.executor.get_engine", return_value=engine):
            execute_sql(SQL, datasource="hinted")
        conn = _conn(engine)
        conn.execution_options.assert_called_once_with(stream_results=True)
        conn.exec_driver_sql.return_value.fetchmany.assert_called_once_with(5)

    def test_a_statement_that_cannot_be_rewritten_runs_as_given(self, two_sources):
        # Not a query: the rewriter declines, the executor still executes it
        # exactly as the caller supplied it (the guard is what refuses writes).
        engine = _run("SELECT FROM WHERE", datasource="hinted")
        assert _sent(engine) == [("SELECT FROM WHERE",)]
