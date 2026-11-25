# SPDX-License-Identifier: Apache-2.0
"""An organisation's Temporal data keys: `temporal_data_keys` (ADR 0225)

One row per random data key that seals an organisation's Temporal payloads, wrapped by the deployment's
key wrapper (ADR 0103: the `TRANIBOR_SECRETS_KEKS` keyring, or Vault transit through its extension).
`state` is `active`, `retired` (rotated out, still opens) or `shredded` (the wrapped key is gone, a
tombstone that keeps the id, the time and who did it). Forced RLS on the organisation, like `org_secrets`.
The organisation's cascade deletes its rows: deleting an organisation shreds its Temporal keys.

The downgrade drops the table, and with it every organisation's data key: the payloads sealed under them
can no longer be read (the same effect as shredding every organisation). Downgrade only a deployment that
never sealed a payload under a data key, or whose histories under one have all closed and aged out.

Revision ID: 31d9f2433719
Revises: 6eb670a63a1b
Create Date: 2026-11-25 11:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "31d9f2433719"
down_revision: str | None = "6eb670a63a1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "temporal_data_keys"


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=True),
        sa.Column("key_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("kek_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("shredded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shredded_by", sa.String(length=200), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_temporal_data_keys_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_temporal_data_keys")),
        sa.UniqueConstraint("org_id", "key_id", name="uq_temporal_data_keys_org_key"),
    )
    op.create_index(op.f("ix_temporal_data_keys_org_id"), TABLE, ["org_id"], unique=False)
    if _postgres():
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {TABLE}_org_isolation ON {TABLE}
            USING (
              '*' = ANY(choregos_current_orgs())
              OR choregos_org_slug(org_id) = ANY(choregos_current_orgs())
            )
            """
        )


def downgrade() -> None:
    if _postgres():
        op.execute(f"DROP POLICY IF EXISTS {TABLE}_org_isolation ON {TABLE}")
    op.drop_index(op.f("ix_temporal_data_keys_org_id"), table_name=TABLE)
    op.drop_table(TABLE)
