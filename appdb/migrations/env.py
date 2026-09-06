# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Alembic environment for the application database (admin panel, phase 2).

Deliberately resolves its own database URL rather than reading
``alembic.ini``'s ``sqlalchemy.url`` (left blank there — see that file's
comment): :func:`appdb.engine.resolve_app_db_url` is exactly the same
``APP_DB_URL``/``APP_DB_SQLITE_PATH`` resolution the running application
itself uses, so a migration always targets the database this deployment's
``.env`` actually names, never a value that could silently drift out of
sync with it.

Two ways to run this
---------------------
``alembic upgrade head``
    Applies pending migrations directly against the resolved application
    database. Refuses via :func:`appdb.engine.raise_if_same_database` if
    that resolves to the same server+database as the read-only warehouse
    connection (``DB_CONNECTION_URL``) — the same check
    ``api/server.py``'s ``lifespan`` runs at every start-up.
``alembic upgrade head --sql``
    Alembic's own "offline" mode: emits the DDL to stdout without
    executing it, for an organisation whose schema changes go through
    change control (phase 2 spec §1.1's "emit the DDL without executing
    it"). No database connection is opened at all in this mode.

:data:`target_metadata` points at :data:`appdb.models.metadata` — the
same table definitions :func:`appdb.models.create_all` uses for the
zero-configuration SQLite path, so the two can never define the schema
differently. It deliberately does NOT include
:mod:`session.persistence`'s own tables — see that module's docstring for
why session storage keeps its own, separate SQLAlchemy engine and is
therefore out of scope for this migration environment.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

import appdb.models as appdb_models
import config as cfg
from appdb.engine import raise_if_same_database, resolve_app_db_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = appdb_models.metadata


def run_migrations_offline() -> None:
    """Emit DDL to stdout without executing it (``alembic upgrade head --sql``)."""
    url = resolve_app_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations directly against the resolved application database."""
    url = resolve_app_db_url()
    raise_if_same_database(url, cfg.settings.db_connection_url)

    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
