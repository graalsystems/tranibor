# SPDX-License-Identifier: Apache-2.0
"""The audit log may forget a prefix, behind a checkpoint written first (ADR 0221)

ADR 0182 made the log append-only: the one `DELETE` the trigger lets through is an organisation's own
cascade. A log that never forgets cannot meet a retention rule, so this revision opens a second, narrow
way out, and nothing else changes:

- `audit_checkpoints` gets two nullable columns, `pruned` (how many entries the checkpoint's prune removed)
  and `retention_days` (the rule that was applied), for the new kind `prune`: the place and hash of the last
  entry removed, from which a walk of what remains starts;
- `choregos_audit_append_only()` lets a `DELETE` of an `audit_log` entry through when a `prune` checkpoint of
  the same chain, at or after the entry's place, **already exists** (it is visible to the deleting
  transaction, so it was written first or in that transaction). `UPDATE` stays refused, `audit_checkpoints`
  stays append-only (a prune checkpoint is never deleted, only its organisation's cascade takes it), and a
  session that cannot read the chain's checkpoints under row-level security (an organisation's, for the
  platform's chain; any, for another organisation's) is refused as before.

The API's role owns the tables, so the limit ADR 0182 names stands: with DDL it can write a checkpoint and
delete behind it. What that hand cannot do is leave the chain looking intact: the walk of what remains starts
from the checkpoint, and the checkpoint must be recorded by an `audit.retention.prune` entry inside the chain
(`audit_chain.ChainWalker.finish`).

The downgrade restores the trigger function and drops the two columns. It does not put back what was
pruned, and it keeps the `prune` rows (the table is append-only): an older verifier then reports the removed
prefix as deleted entries. Downgrade only a deployment that never pruned.

Revision ID: e8c0a2d4f6b1
Revises: 7d2b9f4e6a10
Create Date: 2026-11-24 11:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8c0a2d4f6b1"
down_revision: str | None = "7d2b9f4e6a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The function of ADR 0182 plus the prune clause: an entry goes only behind a checkpoint of its chain that
#: already vouches for it (`seq` at or after the entry's), visible to the deleting transaction.
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION choregos_audit_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 AND OLD.org_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = OLD.org_id) THEN
    RETURN OLD;
  END IF;
  IF TG_OP = 'DELETE' AND TG_TABLE_NAME = 'audit_log' AND OLD.seq IS NOT NULL
     AND EXISTS (SELECT 1 FROM audit_checkpoints c
                 WHERE c.kind = 'prune' AND c.org_id IS NOT DISTINCT FROM OLD.org_id AND c.seq >= OLD.seq) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '%: the audit log is append-only, % refused (ADR 0182)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END
$$ LANGUAGE plpgsql;
"""

#: ADR 0182's function, as the downgrade puts it back.
PREVIOUS_FUNCTION = """
CREATE OR REPLACE FUNCTION choregos_audit_append_only() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 AND OLD.org_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = OLD.org_id) THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '%: the audit log is append-only, % refused (ADR 0182)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("audit_checkpoints", sa.Column("pruned", sa.BigInteger(), nullable=True))
    op.add_column("audit_checkpoints", sa.Column("retention_days", sa.Integer(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(APPEND_ONLY_FUNCTION)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(PREVIOUS_FUNCTION)
    op.drop_column("audit_checkpoints", "retention_days")
    op.drop_column("audit_checkpoints", "pruned")
