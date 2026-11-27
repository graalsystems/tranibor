# SPDX-License-Identifier: Apache-2.0
"""A governed action a demonstration history import proposed is marked imported (ADR 0216, point 14)

`actions` gains `imported`: true on an action an import proposed, false on every other, including every
action that exists today. Deciding an imported action records the decision and plays nothing, and the
actions' relay never starts one.

Revision ID: 4c8e1a7d2f90
Revises: 31d9f2433719
Create Date: 2026-11-27 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8e1a7d2f90"
down_revision: str | None = "31d9f2433719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("imported", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("actions", "imported")
