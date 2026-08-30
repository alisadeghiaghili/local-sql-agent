"""Conftest for the opt-in, live-database integration suite.

``tests/conftest.py``'s ``_no_real_database`` autouse fixture refuses any
real SQLAlchemy engine construction across the whole suite, on purpose —
see its docstring: the default ``DB_CONNECTION_URL`` resolves the literal
host ``server``, and a real attempt blocks on DNS plus the ODBC login
timeout for ~21 seconds.

Tests under ``tests/integration/`` are the one deliberate, documented
exception: :mod:`tests.integration.test_executor_live` needs a REAL
engine to verify Phase 1's ``database/executor.py`` changes actually work
against a live SQL Server, which was never possible during development
(no SQL Server was available then). Rather than weakening the guard for
every other test in the repository, this conftest overrides the fixture
**only for tests collected under this directory** by redefining a fixture
of the same name as a no-op — pytest resolves fixtures by proximity (the
closest ``conftest.py`` to the test wins), so this shadows the parent
conftest's version here and nowhere else.

Every test that actually needs a live database is still individually
opt-in and skips cleanly with no database configured — see
``test_executor_live.py``'s ``live_engine`` fixture — so simply
collecting this package (e.g. as part of a full ``pytest tests/`` run)
never opens a real connection or hangs.
"""

from __future__ import annotations

from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_real_database() -> Iterator[None]:
    """Deliberately a no-op — see the module docstring.

    Named identically to ``tests/conftest.py``'s autouse fixture so it
    shadows it for every test collected under ``tests/integration/``.
    Safe because every test here is independently opt-in and skips
    cleanly (rather than hanging) when no live database is configured.
    """
    yield
