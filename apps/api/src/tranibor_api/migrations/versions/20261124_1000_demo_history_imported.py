# SPDX-License-Identifier: Apache-2.0
"""A demonstration history is marked as imported on the rows it writes (ADR 0216)

`work_items`, `runs`, `findings` and `cost_ledger` gain `imported`: true on a row that a demonstration
history import wrote, false on every other, including every row that exists today. A cost line that
nothing measured can then never be mistaken for one that was.

Revision ID: 7d2b9f4e6a10
Revises: 13e34c50cb88
Create Date: 2026-11-24 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2b9f4e6a10"
down_revision: str | None = "13e34c50cb88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("work_items", "runs", "findings", "cost_ledger")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table, sa.Column("imported", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_index("ix_cost_ledger_imported", "cost_ledger", ["imported"])


def downgrade() -> None:
    op.drop_index("ix_cost_ledger_imported", table_name="cost_ledger")
    for table in TABLES:
        op.drop_column(table, "imported")
