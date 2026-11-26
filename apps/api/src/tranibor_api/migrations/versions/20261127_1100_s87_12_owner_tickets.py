# SPDX-License-Identifier: Apache-2.0
"""The owner tickets taken: `owner_tickets` (ADR 0228 §4, S87-12)

`POST /auth/owner` records each owner ticket's `jti`, unique, in the transaction that opens the session: a
ticket opens one session, whatever the replica that receives it. Forced RLS, platform scope only, like
`device_authorizations`: the exchange has no principal yet. The downgrade drops the table; a ticket taken
before it, still within its 60 seconds, could then be exchanged once more.

Revision ID: 2d487d9bc483
Revises: 8a00629d828e
Create Date: 2026-11-27 09:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d487d9bc483"
down_revision: str | None = "8a00629d828e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "owner_tickets"
WHOLE = "'*' = ANY(choregos_current_orgs())"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("jti", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_tickets")),
        sa.UniqueConstraint("jti", name=op.f("uq_owner_tickets_jti")),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {TABLE}_platform ON {TABLE} USING ({WHOLE}) WITH CHECK ({WHOLE})")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {TABLE}_platform ON {TABLE}")
    op.drop_table(TABLE)
