# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 3: the project_config/ bundle version history.

Revision ID: 0002_config_bundle_versions
Revises: 0001_admin_key_store_and_roles
Create Date: 2026-09-06

Hand-authored to match ``appdb/models.py``'s ``config_bundle_versions``
table exactly -- see that table's own comment for the shape and reasoning
behind each column. Only additive: this migration does not touch
``admin_api_keys`` or ``admin_principal_roles``.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002_config_bundle_versions"
down_revision: Union[str, Sequence[str], None] = "0001_admin_key_store_and_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "config_bundle_versions",
        sa.Column("version_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("based_on_version", sa.Integer(), nullable=True),
        sa.Column("restored_from_version", sa.Integer(), nullable=True),
        sa.Column("restored_file", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_by_capability", sa.String(length=32), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.String(length=64), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("diff_json", sa.Text(), nullable=True),
        sa.Column("dry_run_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("config_bundle_versions")
