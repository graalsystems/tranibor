# SPDX-License-Identifier: Apache-2.0
"""The installation's posture, recorded once (ADR 0228 §2, §9).

One row, written by the API's first start (`tranibor_api.personal_installation`): `personal` for one
person's own installation (`TRANIBOR_ENV=personal`), `organisation` for every other. Nothing changes it.
When it says `personal`, the database refuses a membership for a second person, whatever path writes it:
on PostgreSQL the trigger `choregos_sole_member()` the migration `20261127_1000_s87_10_sole_member.py`
lays; on SQLite, the suite's and a developer's database, the triggers below, which `create_all()` lays as
that migration lays them (`apps/api/tests/test_personal_single_member.py` compares the two).

SQLite's triggers are dropped with their table: an Alembic batch operation that rebuilds `memberships` on
SQLite must lay them again, or that comparison turns red.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Connection, DateTime, Integer, String, event, func
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, utcnow

#: The two postures (the contract's `AuthMethods.posture`).
ORGANISATION = "organisation"
PERSONAL = "personal"

#: What every refusal of a second person says, whichever database writes it: the API recognises it.
SOLE_MEMBER_MARK = "choregos_sole_member"


class Installation(Base):
    """The posture this database was first started in, and in which `TRANIBOR_ENV` (ADR 0228 §2)."""

    __tablename__ = "installation"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(f"posture IN ('{ORGANISATION}', '{PERSONAL}')", name="posture"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    posture: Mapped[str] = mapped_column(String(16))
    #: `TRANIBOR_ENV` at the first start, for the record: the rules read the posture.
    environment: Mapped[str] = mapped_column(String(32))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )


#: A person (not a machine account, ADR 0131) other than the row's, holding a membership (another row).
_SECOND_PERSON = (
    "EXISTS (SELECT 1 FROM installation WHERE posture = 'personal') "
    "AND NOT coalesce((SELECT is_service FROM users WHERE id = NEW.user_id), 0) "
    "AND EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id "
    "WHERE m.user_id <> NEW.user_id AND m.id <> NEW.id AND NOT u.is_service)"
)
_SOLE_MEMBER_REFUSAL = (
    f"SELECT RAISE(ABORT, '{SOLE_MEMBER_MARK}: this installation is personal and holds one person "
    "(ADR 0228): a membership for a second person is refused');"
)
_WRITTEN_ONCE_REFUSAL = "SELECT RAISE(ABORT, 'installation: the posture is written once (ADR 0228)');"

#: SQLite's triggers, as the migration writes them (`CREATE TRIGGER IF NOT EXISTS` here: `create_all()` may
#: run again over the same file, and SQLite stores the statement without the clause).
SQLITE_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER IF NOT EXISTS memberships_sole_member_insert BEFORE INSERT ON memberships "
    f"FOR EACH ROW WHEN {_SECOND_PERSON} BEGIN {_SOLE_MEMBER_REFUSAL} END",
    "CREATE TRIGGER IF NOT EXISTS memberships_sole_member_update BEFORE UPDATE ON memberships "
    f"FOR EACH ROW WHEN {_SECOND_PERSON} BEGIN {_SOLE_MEMBER_REFUSAL} END",
    "CREATE TRIGGER IF NOT EXISTS installation_written_once_update BEFORE UPDATE ON installation "
    f"FOR EACH ROW BEGIN {_WRITTEN_ONCE_REFUSAL} END",
    "CREATE TRIGGER IF NOT EXISTS installation_written_once_delete BEFORE DELETE ON installation "
    f"FOR EACH ROW BEGIN {_WRITTEN_ONCE_REFUSAL} END",
)


@event.listens_for(Base.metadata, "after_create")
def _lay_the_sqlite_triggers(_metadata: Any, connection: Connection, **_kw: Any) -> None:
    """After every table, since the triggers read `users`, `memberships` and `installation`."""
    if connection.dialect.name == "sqlite":
        for statement in SQLITE_TRIGGERS:
            connection.exec_driver_sql(statement)
