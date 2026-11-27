# SPDX-License-Identifier: Apache-2.0
"""The door's runs lane: `outpost_runs`, an offer claimed once (S87-16, ADR 0226 §4-5)

A run placed on a person's runner is a row of `outpost_runs`: its id is the run's (or a conversation turn's),
its outpost the work owner's personal outpost, its state `offered` until one instance claims it, then
`claimed`, `ended`, `lost` or `cancelled`; `expired` when nobody claimed it within its queue bound. It holds
what the run token minted at the claim names (`token_subject`, `project_slug`, `work_item_key`) and never the
token, which travels in the sealed envelope only; `env` is the runner's environment beyond its id, address
and token, never a secret.

Row-level security, as an outpost's other own rows:

- forced, with `outpost_runs_org_isolation` as `outpost_calls` has it: the session's organisation scope holds
  the row's organisation, and the session names the row's outpost (`app.current_outpost`), so a session that
  names none sees none of them, the platform's scope included;
- `outpost_runs_personal_owner`, RESTRICTIVE, as `20261127_1200_s87_15_personal_outposts` wrote it for
  `outpost_calls`: a personal outpost's offers exist under the platform's scope (`*`), for the door, which
  marks its own sessions (`app.current_door = 'outposts'`), and for a session of the outpost's owner; for no
  other person, and for no session that merely names no person.

A new table, no row written: nothing to scope. The downgrade drops it, and with it every offer: a run that
waits for a person's runner then waits for nothing, and its queue bound closes it.

Revision ID: 5c1e9a7d3b20
Revises: 42cc5be65e2b
Create Date: 2026-11-27 14:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from tranibor_api.db.base import Json

revision: str = "5c1e9a7d3b20"
down_revision: str | None = "42cc5be65e2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "outpost_runs"
ORGANISATION = (
    "('*' = ANY(choregos_current_orgs()) OR choregos_org_slug(org_id) = ANY(choregos_current_orgs()))"
)
ORG_ISOLATION = f"""
CREATE POLICY {TABLE}_org_isolation ON {TABLE}
USING (
  {ORGANISATION}
  AND outpost_id = choregos_current_outpost()
)
"""
OWNER_ONLY = """
CREATE POLICY outpost_runs_personal_owner ON outpost_runs AS RESTRICTIVE
USING (
  '*' = ANY(choregos_current_orgs())
  OR current_setting('app.current_door', true) = 'outposts'
  OR EXISTS (
    SELECT 1 FROM outposts
    WHERE outposts.id = outpost_runs.outpost_id
      AND (outposts.owner_user_id IS NULL OR outposts.owner_user_id = choregos_utilisateur_courant())
  )
)
"""


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("org_id", sa.String(length=36), nullable=False),
        sa.Column("outpost_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="offered", nullable=False),
        sa.Column("credential", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("isolation", sa.String(length=16), nullable=False),
        sa.Column("env", Json(), nullable=False),
        sa.Column("api_url", sa.String(length=2000), nullable=False),
        sa.Column("token_subject", sa.String(length=200), nullable=False),
        sa.Column("project_slug", sa.String(length=200), nullable=False),
        sa.Column("work_item_key", sa.String(length=512), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=True),
        sa.Column("max_minutes", sa.Integer(), nullable=False),
        sa.Column("lease_s", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("cancel_told_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f(f"fk_{TABLE}_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outpost_id"], ["outposts.id"], name=op.f(f"fk_{TABLE}_outpost_id_outposts"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f(f"fk_{TABLE}_project_id_projects"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["outpost_instances.id"],
            name=op.f(f"fk_{TABLE}_instance_id_outpost_instances"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(op.f(f"ix_{TABLE}_org_id"), TABLE, ["org_id"], unique=False)
    op.create_index(op.f(f"ix_{TABLE}_project_id"), TABLE, ["project_id"], unique=False)
    op.create_index(op.f(f"ix_{TABLE}_instance_id"), TABLE, ["instance_id"], unique=False)
    op.create_index(f"ix_{TABLE}_outpost_state", TABLE, ["outpost_id", "state"], unique=False)
    if not _postgres():
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(ORG_ISOLATION)
    op.execute(OWNER_ONLY)


def downgrade() -> None:
    if _postgres():
        op.execute(f"DROP POLICY IF EXISTS {TABLE}_personal_owner ON {TABLE}")
        op.execute(f"DROP POLICY IF EXISTS {TABLE}_org_isolation ON {TABLE}")
    op.drop_index(f"ix_{TABLE}_outpost_state", table_name=TABLE)
    for column in ("instance_id", "project_id", "org_id"):
        op.drop_index(op.f(f"ix_{TABLE}_{column}"), table_name=TABLE)
    op.drop_table(TABLE)
