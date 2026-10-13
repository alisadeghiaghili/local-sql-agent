# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Analyst confirmations: "this answer is correct".

Revision ID: 0005_turn_confirmations
Revises: 0004_access_requests
Create Date: 2026-10-13

Hand-authored to match ``appdb/models.py``'s ``turn_confirmations`` table
exactly -- see that table's own comment for the shape and reasoning behind
each column. Only additive: this migration does not touch any earlier
table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005_turn_confirmations"
down_revision: Union[str, Sequence[str], None] = "0004_access_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_confirmations",
        sa.Column("confirmation_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "session_id", "turn_id", "principal_id",
            name="uq_turn_confirmations_turn_principal",
        ),
    )


def downgrade() -> None:
    op.drop_table("turn_confirmations")
