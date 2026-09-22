# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1 -- the ``0004_access_requests`` Alembic migration itself.

Drives real Alembic ``upgrade``/``downgrade`` commands (via a
:class:`alembic.config.Config` pointed at this repo's real
``appdb/migrations`` script directory) against a real, empty SQLite file on
a real ``tmp_path``. No mock at the boundary under test: this proves the
hand-authored migration script itself is runnable in both directions, not
just that ``appdb.models.metadata.create_all`` (the zero-configuration
path) agrees with it.

``appdb/migrations/env.py`` deliberately resolves its OWN database URL via
``appdb.engine.resolve_app_db_url()`` (reading ``cfg.settings.app_db_url``)
rather than the Alembic ``Config``'s own ``sqlalchemy.url`` -- see that
module's docstring. Every test below therefore points migrations at
``tmp_path`` through :func:`config.override_settings(app_db_url=...)`,
never through ``Config.set_main_option("sqlalchemy.url", ...)``, which
``env.py`` would silently ignore.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect

import config as cfg

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(_REPO_ROOT / "appdb" / "migrations"))
    return alembic_cfg


def test_upgrade_to_0004_creates_access_requests_with_the_expected_columns(tmp_path):
    db_path = tmp_path / "app.db"
    db_url = f"sqlite:///{db_path}"
    alembic_cfg = _alembic_config()

    with cfg.override_settings(app_db_url=db_url):
        command.upgrade(alembic_cfg, "0004_access_requests")

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert insp.has_table("access_requests")
        assert insp.has_table("turn_feedback"), "0004 must not skip earlier revisions"
        columns = {c["name"] for c in insp.get_columns("access_requests")}
        assert columns == {
            "request_id", "session_id", "turn_id", "requester_principal_id",
            "column_name", "created_at", "status", "resolution_note",
            "resolved_by", "resolved_at",
        }
    finally:
        engine.dispose()


def test_upgrade_then_downgrade_removes_only_access_requests(tmp_path):
    db_path = tmp_path / "app.db"
    db_url = f"sqlite:///{db_path}"
    alembic_cfg = _alembic_config()

    with cfg.override_settings(app_db_url=db_url):
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "0003_turn_feedback")

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert not insp.has_table("access_requests"), "downgrade must drop access_requests"
        assert insp.has_table("turn_feedback"), "downgrade to 0003 must not touch earlier tables"
    finally:
        engine.dispose()


def test_upgrade_to_head_from_scratch_reaches_0004(tmp_path):
    """The ordinary path a fresh deployment takes -- upgrade all the way
    to head in one call, from an empty database."""
    db_path = tmp_path / "app.db"
    db_url = f"sqlite:///{db_path}"
    alembic_cfg = _alembic_config()

    with cfg.override_settings(app_db_url=db_url):
        command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        for table in ("admin_api_keys", "admin_principal_roles", "config_bundle_versions",
                      "turn_feedback", "access_requests"):
            assert insp.has_table(table), f"expected {table!r} to exist at head"
    finally:
        engine.dispose()


def test_round_trip_down_to_base_and_back_up(tmp_path):
    """A full downgrade to nothing and back up must be idempotent-safe and
    leave the same table set as a single upgrade to head."""
    db_path = tmp_path / "app.db"
    db_url = f"sqlite:///{db_path}"
    alembic_cfg = _alembic_config()

    with cfg.override_settings(app_db_url=db_url):
        command.upgrade(alembic_cfg, "head")
        command.downgrade(alembic_cfg, "base")

        engine = create_engine(db_url)
        try:
            insp = inspect(engine)
            for table in ("admin_api_keys", "admin_principal_roles", "config_bundle_versions",
                          "turn_feedback", "access_requests"):
                assert not insp.has_table(table), f"expected {table!r} to be gone after downgrade to base"
        finally:
            engine.dispose()

        command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert insp.has_table("access_requests")
    finally:
        engine.dispose()
