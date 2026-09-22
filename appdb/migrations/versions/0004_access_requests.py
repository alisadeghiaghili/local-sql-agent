# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1: "Request access" from a denied-column guard rejection.

Revision ID: 0004_access_requests
Revises: 0003_turn_feedback
Create Date: 2026-09-22

Hand-authored to match ``appdb/models.py``'s ``access_requests`` table
exactly -- see that table's own comment for the shape and reasoning behind
each column. Only additive: this migration does not touch any earlier
table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004_access_requests"
down_revision: Union[str, Sequence[str], None] = "0003_turn_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "access_requests",
        sa.Column("request_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("requester_principal_id", sa.String(length=255), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("access_requests")
