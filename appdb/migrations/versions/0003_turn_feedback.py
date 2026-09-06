# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 4: wrong-answer feedback and its triage.

Revision ID: 0003_turn_feedback
Revises: 0002_config_bundle_versions
Create Date: 2026-09-06

Hand-authored to match ``appdb/models.py``'s ``turn_feedback`` table
exactly -- see that table's own comment for the shape and reasoning behind
each column. Only additive: this migration does not touch any earlier
table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003_turn_feedback"
down_revision: Union[str, Sequence[str], None] = "0002_config_bundle_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_feedback",
        sa.Column("feedback_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("reporter_principal_id", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("config_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution_outcome", sa.String(length=32), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolution_config_version_id", sa.Integer(), nullable=True),
        sa.Column("resolution_golden_case_id", sa.String(length=255), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("turn_feedback")
