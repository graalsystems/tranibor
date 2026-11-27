# SPDX-License-Identifier: Apache-2.0
"""A machine account holding a membership does not become a second person (S87-10, ADR 0228 §2)

`choregos_sole_member()` (migration `8a00629d828e`) refuses a membership for a second person and accepts a
machine account's (`users.is_service`, ADR 0131). Turning that account into a person afterwards
(`UPDATE users SET is_service = false`) left two persons holding a membership, and nothing refused the
write: only the API's next start did. Now:

- **`choregos_sole_member_flip()`**, a `BEFORE UPDATE OF is_service` trigger on `users`, applies the same
  rule: when the `installation` row says `personal`, a user who holds a membership and becomes a person
  (`is_service` from true to false) while another person holds one is refused (`check_violation`, its
  message starting with `choregos_sole_member`, which the API answers with its 409 `one_person`), whoever
  writes it. It reads `users` and `memberships` past the session's row-level security scope, which it opens
  to `*` for its reads and gives back, and it locks the installation's row first, as the membership trigger
  does.
- **`choregos_sole_member()`** now locks the installation's row before it reads whether the new member is a
  machine account, for every membership written in the posture, not only a person's: a membership given to
  a machine account and that account turned into a person, written at once, are then put in a line, and the
  second reads what the first committed. Before, the membership trigger read `is_service` without the lock
  and the two could both pass.
- On **SQLite** (the suite's and a developer's database), the trigger `users_sole_member_flip`, which
  `create_all()` lays too (`tranibor_api.db.models.installation`); SQLite runs one writer at a time.

The downgrade drops the new trigger and its function and puts `choregos_sole_member()` back as `8a00629d828e`
wrote it.

Revision ID: 42cc5be65e2b
Revises: 8915b7b16adf
Create Date: 2026-11-27 13:10:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "42cc5be65e2b"
down_revision: str | None = "8915b7b16adf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A user who holds a membership turned into a person while another person holds one: refused. The scope is
#: opened for the reads only, and the installation's row locked before them.
FLIP_FUNCTION = """
CREATE OR REPLACE FUNCTION choregos_sole_member_flip() RETURNS trigger AS $$
DECLARE
  scope text;
  other text;
BEGIN
  IF NEW.is_service OR NOT OLD.is_service THEN
    RETURN NEW;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM installation WHERE posture = 'personal') THEN
    RETURN NEW;
  END IF;
  scope := current_setting('app.current_orgs', true);
  PERFORM set_config('app.current_orgs', '*', true);
  PERFORM 1 FROM installation FOR UPDATE;
  IF EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = NEW.id) THEN
    SELECT m.user_id INTO other FROM memberships m JOIN users u ON u.id = m.user_id
      WHERE m.user_id <> NEW.id AND NOT u.is_service
      LIMIT 1;
  END IF;
  PERFORM set_config('app.current_orgs', coalesce(scope, ''), true);
  IF other IS NOT NULL THEN
    RAISE EXCEPTION 'choregos_sole_member: this installation is personal and holds one person (ADR 0228): '
      'user % already holds a membership, user % holds one and may not become a person', other, NEW.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;
"""

#: The membership rule, the installation's row locked before `is_service` is read.
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
  PERFORM 1 FROM installation FOR UPDATE;
  IF NOT coalesce((SELECT u.is_service FROM users u WHERE u.id = NEW.user_id), false) THEN
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

#: `choregos_sole_member()` as `8a00629d828e` wrote it, for the downgrade.
SOLE_MEMBER_FUNCTION_BEFORE = """
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

#: SQLite: the same rule, as `tranibor_api.db.models.installation.SQLITE_TRIGGERS` lays it for `create_all()`.
SQLITE_TRIGGER = "users_sole_member_flip"
SQLITE_BODY = (
    "BEFORE UPDATE OF is_service ON users FOR EACH ROW WHEN OLD.is_service AND NOT NEW.is_service "
    "AND EXISTS (SELECT 1 FROM installation WHERE posture = 'personal') "
    "AND EXISTS (SELECT 1 FROM memberships WHERE user_id = NEW.id) "
    "AND EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id "
    "WHERE m.user_id <> NEW.id AND NOT u.is_service) "
    "BEGIN SELECT RAISE(ABORT, 'choregos_sole_member: this installation is personal and holds one person "
    "(ADR 0228): a user holding a membership may not become a second person'); END"
)


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres():
        op.execute(f"CREATE TRIGGER {SQLITE_TRIGGER} {SQLITE_BODY}")
        return
    op.execute(SOLE_MEMBER_FUNCTION)
    op.execute(FLIP_FUNCTION)
    op.execute(
        "CREATE TRIGGER users_sole_member_flip BEFORE UPDATE OF is_service ON users "
        "FOR EACH ROW EXECUTE FUNCTION choregos_sole_member_flip()"
    )


def downgrade() -> None:
    if not _postgres():
        op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_TRIGGER}")
        return
    op.execute("DROP TRIGGER IF EXISTS users_sole_member_flip ON users")
    op.execute("DROP FUNCTION IF EXISTS choregos_sole_member_flip()")
    op.execute(SOLE_MEMBER_FUNCTION_BEFORE)
