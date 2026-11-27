# SPDX-License-Identifier: Apache-2.0
"""Personal outposts: `outposts.owner_user_id`, `outposts.paused_by` and the owner's policy (S87-15, ADR 0226)

A person's runner is an outpost of the organisation that one person owns (ADR 0226 §1): `owner_user_id`
names them, and a unique index on (`org_id`, `owner_user_id`) keeps one per person and organisation, as
`uq_projects_personal_owner` keeps one personal space. Every existing outpost is an organisation's: the
column is null for all of them. `paused_by` records which side paused an outpost, `administrator` or
`owner`, which is the side that lifts the pause (ADR 0226 §2); null for every existing row, which an
administrator paused if it is paused, and which an administrator therefore resumes.

The `outposts` row stays an organisation row, under `outposts_org_isolation`: its administrators list,
pause, revoke and delete it. Its own rows (`outpost_instances`, `outpost_enrolments`, `outpost_calls`) get
a second, RESTRICTIVE policy copied from `projects_personal_owner`, so it narrows what
`<table>_org_isolation` allows instead of widening it. A row of a personal outpost is seen and written:

- under the platform scope (`*`);
- by the door, which names the one outpost its caller's signature resolves to (ADR 0148 §6) and works for
  an instance, never for a person. It is known by a marker its own code sets in each of its sessions,
  `app.current_door = 'outposts'` (`db.session.mark_the_door`, `outposts/door.py`, served by the API or
  alone), and never by the absence of a person: a session that names no person and is not the door sees
  none of these rows;
- by a session of its owner (`app.current_user`);

and by no other person, whatever their role and the outpost they name. The outpost is looked up under
row-level security: a row whose outpost the session cannot see is hidden, it is never taken for an
organisation's. A policy without `WITH CHECK` judges writes with the same expression.

The downgrade drops the policies, the index and the columns. A personal outpost would then pass for an
organisation's, which its administrators could enrol machines into: it deletes them first, with their
instances, tokens and calls.

Revision ID: 8915b7b16adf
Revises: 2d487d9bc483
Create Date: 2026-11-27 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8915b7b16adf"
down_revision: str | None = "2d487d9bc483"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A personal outpost's own rows.
OUTPOST_ROWS = ("outpost_instances", "outpost_enrolments", "outpost_calls")


def _owner_only(table: str) -> str:
    return f"""
    CREATE POLICY {table}_personal_owner ON {table} AS RESTRICTIVE
    USING (
      '*' = ANY(choregos_current_orgs())
      OR current_setting('app.current_door', true) = 'outposts'
      OR EXISTS (
        SELECT 1 FROM outposts
        WHERE outposts.id = {table}.outpost_id
          AND (outposts.owner_user_id IS NULL OR outposts.owner_user_id = choregos_utilisateur_courant())
      )
    )
    """


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table("outposts") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("paused_by", sa.String(length=16), nullable=True))
        batch.create_foreign_key(
            batch.f("fk_outposts_owner_user_id_users"), "users", ["owner_user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index(batch.f("ix_outposts_owner_user_id"), ["owner_user_id"], unique=False)
    op.create_index(
        "uq_outposts_personal_owner",
        "outposts",
        ["org_id", "owner_user_id"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
        sqlite_where=sa.text("owner_user_id IS NOT NULL"),
    )
    if _postgres():
        for table in OUTPOST_ROWS:
            op.execute(_owner_only(table))


def downgrade() -> None:
    if _postgres():
        for table in OUTPOST_ROWS:
            op.execute(f"DROP POLICY IF EXISTS {table}_personal_owner ON {table}")
        # The rows of every organisation, under forced RLS (S39-27).
        op.execute("SELECT set_config('app.current_orgs', '*', true)")
    op.execute("DELETE FROM outposts WHERE owner_user_id IS NOT NULL")
    op.drop_index("uq_outposts_personal_owner", table_name="outposts")
    with op.batch_alter_table("outposts") as batch:
        batch.drop_index(batch.f("ix_outposts_owner_user_id"))
        batch.drop_constraint(batch.f("fk_outposts_owner_user_id_users"), type_="foreignkey")
        batch.drop_column("paused_by")
        batch.drop_column("owner_user_id")
