# SPDX-License-Identifier: Apache-2.0
"""A personal installation holds one person, and the database refuses a second (S87-10, ADR 0228 §2)

- **`installation`**, one row (`id = 1`): the posture the API's first start recorded, `personal` or
  `organisation`, and `TRANIBOR_ENV` then. The migration writes no row: a database migrated and never
  started has no posture yet. The row is written once: a `BEFORE UPDATE OR DELETE` trigger
  (`choregos_installation_written_once`) refuses both, and `TRUNCATE` is revoked from every role that holds
  it, the table's owner (the API's role, which runs this migration) included. Under forced row-level
  security: every session reads it (the trigger below reads it from any scope; it holds no organisation's
  data), and only the platform's scope (`*`) writes it.
- **`choregos_sole_member()`**, a `BEFORE INSERT OR UPDATE` trigger on `memberships`, written as the audit
  chain's (`20261117_2300_audit_chain.py`): when the row says `personal`, a membership for a person
  (`users.is_service` false: a machine account, ADR 0131, is not one) while another person holds one is
  refused (`check_violation`, its message starting with `choregos_sole_member`), whatever path writes it.
  It reads `users` and `memberships` past the session's row-level security scope, which it opens to `*`
  for its two reads and gives back: a session bounded to one organisation would otherwise miss a person
  of another. It locks the `installation` row first, so two first sign-ins at once are put in a line and
  the second sees the first.
- On **SQLite** (the suite's and a developer's database), the same rule as four triggers, which
  `create_all()` lays too (`tranibor_api.db.models.installation`); no row-level security there.

The downgrade drops the triggers, the functions and the table: the posture is forgotten, and the next
start of the API of this release records it again from `TRANIBOR_ENV`.

Revision ID: 8a00629d828e
Revises: 31d9f2433719
Create Date: 2026-11-27 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a00629d828e"
down_revision: str | None = "31d9f2433719"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "installation"
WHOLE = "'*' = ANY(choregos_current_orgs())"

#: The rule, on PostgreSQL. `other` is a person other than the row's who holds another membership; the
#: scope is opened for the two reads only, and the installation's row locked before the second.
SOLE_MEMBER_FUNCTION = """
CREATE OR REPLACE FUNCTION choregos_sole_member() RETURNS trigger AS $$
DECLARE
  scope text;
  other text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM installation WHERE posture = 'personal') THEN
    RETURN NEW;
  END IF;
  scope := current_setting('app.current_orgs', true);
  PERFORM set_config('app.current_orgs', '*', true);
  IF NOT coalesce((SELECT u.is_service FROM users u WHERE u.id = NEW.user_id), false) THEN
    PERFORM 1 FROM installation FOR UPDATE;
    SELECT m.user_id INTO other FROM memberships m JOIN users u ON u.id = m.user_id
      WHERE m.user_id <> NEW.user_id AND m.id <> NEW.id AND NOT u.is_service
      LIMIT 1;
  END IF;
  PERFORM set_config('app.current_orgs', coalesce(scope, ''), true);
  IF other IS NOT NULL THEN
    RAISE EXCEPTION 'choregos_sole_member: this installation is personal and holds one person (ADR 0228): '
      'user % already holds a membership, a membership for user % is refused', other, NEW.user_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

WRITTEN_ONCE_FUNCTION = """
CREATE OR REPLACE FUNCTION choregos_installation_written_once() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'installation: the posture is written once, % refused (ADR 0228)', TG_OP
    USING ERRCODE = 'check_violation';
END
$$ LANGUAGE plpgsql;
"""

#: `TRUNCATE` taken from every role that holds it on the table, its owner included (as the audit chain's
#: migration takes it from the log). `UPDATE` stays: the sole-member trigger locks the row `FOR UPDATE`.
REVOKE_TRUNCATE = """
DO $$
DECLARE holder text;
BEGIN
  FOR holder IN
    SELECT DISTINCT pg_get_userbyid(a.grantee) FROM pg_class c,
      aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
    WHERE c.oid = 'installation'::regclass AND a.privilege_type = 'TRUNCATE' AND a.grantee <> 0
  LOOP
    EXECUTE format('REVOKE TRUNCATE ON installation FROM %I', holder);
  END LOOP;
  REVOKE TRUNCATE ON installation FROM PUBLIC;
END
$$;
"""

#: SQLite: the same rule, as `tranibor_api.db.models.installation.SQLITE_TRIGGERS` lays it for `create_all()`.
_SECOND_PERSON = (
    "EXISTS (SELECT 1 FROM installation WHERE posture = 'personal') "
    "AND NOT coalesce((SELECT is_service FROM users WHERE id = NEW.user_id), 0) "
    "AND EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id "
    "WHERE m.user_id <> NEW.user_id AND m.id <> NEW.id AND NOT u.is_service)"
)
_SOLE_MEMBER_REFUSAL = (
    "SELECT RAISE(ABORT, 'choregos_sole_member: this installation is personal and holds one person "
    "(ADR 0228): a membership for a second person is refused');"
)
_WRITTEN_ONCE_REFUSAL = "SELECT RAISE(ABORT, 'installation: the posture is written once (ADR 0228)');"
SQLITE_TRIGGERS = {
    "memberships_sole_member_insert": "BEFORE INSERT ON memberships "
    f"FOR EACH ROW WHEN {_SECOND_PERSON} BEGIN {_SOLE_MEMBER_REFUSAL} END",
    "memberships_sole_member_update": "BEFORE UPDATE ON memberships "
    f"FOR EACH ROW WHEN {_SECOND_PERSON} BEGIN {_SOLE_MEMBER_REFUSAL} END",
    "installation_written_once_update": "BEFORE UPDATE ON installation "
    f"FOR EACH ROW BEGIN {_WRITTEN_ONCE_REFUSAL} END",
    "installation_written_once_delete": "BEFORE DELETE ON installation "
    f"FOR EACH ROW BEGIN {_WRITTEN_ONCE_REFUSAL} END",
}


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("posture", sa.String(length=16), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_installation_singleton")),
        sa.CheckConstraint("posture IN ('organisation', 'personal')", name=op.f("ck_installation_posture")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_installation")),
    )
    if not _postgres():
        for name, body in SQLITE_TRIGGERS.items():
            op.execute(f"CREATE TRIGGER {name} {body}")
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} USING (true) WITH CHECK ({WHOLE})")
    op.execute(REVOKE_TRUNCATE)
    op.execute(WRITTEN_ONCE_FUNCTION)
    op.execute(
        f"CREATE TRIGGER {TABLE}_written_once BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION choregos_installation_written_once()"
    )
    op.execute(SOLE_MEMBER_FUNCTION)
    op.execute(
        "CREATE TRIGGER memberships_sole_member BEFORE INSERT OR UPDATE ON memberships "
        "FOR EACH ROW EXECUTE FUNCTION choregos_sole_member()"
    )


def downgrade() -> None:
    if _postgres():
        op.execute("DROP TRIGGER IF EXISTS memberships_sole_member ON memberships")
        op.execute("DROP FUNCTION IF EXISTS choregos_sole_member()")
        op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_written_once ON {TABLE}")
        op.execute("DROP FUNCTION IF EXISTS choregos_installation_written_once()")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table(TABLE)
