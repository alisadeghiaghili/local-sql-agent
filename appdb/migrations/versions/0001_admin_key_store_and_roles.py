# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 2: the key store and role-grant tables.

Revision ID: 0001_admin_key_store_and_roles
Revises:
Create Date: 2026-09-06

Hand-authored to match ``appdb/models.py`` exactly, table for table and
column for column — that module's docstring explains the shape and
reasoning for each; this migration only emits the DDL. For a fresh SQLite
deployment, ``appdb.engine.get_app_engine`` creates these same two tables
directly (``checkfirst=True``) without ever running this file at all; this
migration exists for a managed backend (PostgreSQL, SQL Server, MySQL)
whose schema changes an organisation wants to go through Alembic and,
optionally, change control (``alembic upgrade head --sql`` emits the DDL
below without executing it).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0001_admin_key_store_and_roles"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_api_keys",
        sa.Column("key_sha256", sa.String(length=64), primary_key=True),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("denied_columns_json", sa.String(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
        sa.Column("disabled_at", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "admin_principal_roles",
        sa.Column("principal_id", sa.String(length=255), primary_key=True),
        sa.Column("capability", sa.String(length=32), primary_key=True),
        sa.Column("granted_at", sa.String(length=64), nullable=False),
        sa.Column("granted_by", sa.String(length=255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_principal_roles")
    op.drop_table("admin_api_keys")
