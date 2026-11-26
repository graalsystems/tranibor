# SPDX-License-Identifier: Apache-2.0
"""A personal installation holds one person, and the database holds the rule (ADR 0228 §2, §9).

Not `services/installation.py`, which installs a template's agents into a project: this module is about
the installation itself, and its posture.

- **The posture is recorded once.** The API's first start writes the `installation` row: `personal` when
  `TRANIBOR_ENV=personal`, `organisation` for every other environment (`dev`, `test`, `staging`, `prod`).
  Nothing changes it afterwards: no route, no command, and the database refuses an update or a deletion.
- **One person.** When the row says `personal`, the database refuses a membership for a second person,
  whatever writes it: the members routes (`routers/admin.py`), a sign-in (`routers/auth.py`), a project or
  an organisation created (`routers/projects.py`), `bootstrap.py`, an extension or a raw `INSERT`
  (`choregos_sole_member()` on PostgreSQL, its SQLite twin in `db/models/installation.py`). A machine
  account (`users.is_service`, ADR 0131) is not a person and is accepted. The API answers such a refusal
  with a 409 whose `code` is `one_person` and whose detail names the record (`install_error_handlers`).
- **The start-up check** (`check_the_installation`, from `main.lifespan`, before the bootstrap) refuses to
  serve when `TRANIBOR_ENV` and the recorded posture differ, either way; when more than one person holds a
  membership in the personal posture; when `TRANIBOR_BOOTSTRAP_ADMINS` names more than one address there;
  and on SQLite in that posture, a development database that no migration follows. A first start that
  is refused records nothing.

Outside the personal posture nothing changes: the row says `organisation`, the trigger lets every
membership through, and the start-up only records it.
"""

from __future__ import annotations

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core.production import ENVIRONMENT_VARIABLE, is_personal

from .config import Settings
from .db.models import Membership, User
from .db.models.installation import ORGANISATION, PERSONAL, SOLE_MEMBER_MARK, Installation
from .db.session import session_scope
from .errors import ApiError
from .logging import get_logger

logger = get_logger("choregos.personal_installation")

#: The `code` of the problem a refused second person gets.
ONE_PERSON = "one_person"
#: The variable that names the bootstrap's administrators.
BOOTSTRAP_ADMINS_VARIABLE = "TRANIBOR_BOOTSTRAP_ADMINS"


class InstallationRefusedError(RuntimeError):
    """The API (or the bootstrap) refuses to serve this database in this posture (ADR 0228)."""


def posture_of(environment: str | None) -> str:
    """The posture `TRANIBOR_ENV` names: `personal` for one person's own installation, else `organisation`."""
    return PERSONAL if is_personal(environment) else ORGANISATION


async def recorded_posture(session: AsyncSession) -> str | None:
    """The posture the first start recorded, or `None` before any start. Any scope reads it."""
    return (await session.execute(select(Installation.posture))).scalar_one_or_none()


async def record_posture(session: AsyncSession, posture: str, *, environment: str) -> str:
    """Record `posture` unless one is recorded already, and return the recorded one.

    Two replicas starting at once both insert; the database keeps the first and the other reads it back.
    The session must be in the platform's scope (`orgs="*"`): only that scope writes the row."""
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    await session.execute(
        insert(Installation)
        .values(id=1, posture=posture, environment=environment)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    recorded = await recorded_posture(session)
    if recorded is None:  # pragma: no cover - the row was just written or kept
        raise InstallationRefusedError("the installation's posture could not be recorded")
    return recorded


async def persons_holding_a_membership(session: AsyncSession) -> list[str]:
    """The addresses of the persons (not machine accounts) who hold a membership, sorted. Read in the
    platform's scope: a person of any organisation counts."""
    rows = await session.execute(
        select(func.lower(User.email))
        .join(Membership, Membership.user_id == User.id)
        .where(User.is_service.is_(False))
        .distinct()
        .order_by(func.lower(User.email))
    )
    return [str(email) for email in rows.scalars()]


def refuse_what_the_settings_forbid(settings: Settings) -> None:
    """The personal posture's refusals that read the settings alone: SQLite, two bootstrap addresses."""
    if posture_of(settings.env) != PERSONAL:
        return
    if settings.is_sqlite:
        raise InstallationRefusedError(
            f"{ENVIRONMENT_VARIABLE}=personal on SQLite: refused. A personal installation is one person's "
            "production (ADR 0228): its database is PostgreSQL, migrated by "
            "`python -m tranibor_api.migrate`, under row-level security and the audit chain. SQLite is a "
            "development database that no migration follows: set TRANIBOR_DATABASE_URL to the "
            "installation's PostgreSQL."
        )
    admins = settings.bootstrap_admin_emails
    if len(admins) > 1:
        raise InstallationRefusedError(
            f"{BOOTSTRAP_ADMINS_VARIABLE} names {len(admins)} addresses and {ENVIRONMENT_VARIABLE}=personal: "
            "refused. A personal installation holds one person (ADR 0228): name that person's address "
            "alone, or start an organisation installation."
        )


def _refuse_more_than_one(persons: list[str], *, first_start: bool) -> None:
    if len(persons) <= 1:
        return
    recorded = (
        "and records nothing: the database stays unclaimed"
        if first_start
        else "its `installation` row having recorded `personal` at its first start"
    )
    raise InstallationRefusedError(
        f"{ENVIRONMENT_VARIABLE}=personal and {len(persons)} persons hold a membership "
        f"({', '.join(persons)}): the API refuses to start, {recorded}. A personal installation holds one "
        "person (ADR 0228): remove the others' memberships, or serve this database as an organisation "
        "installation."
    )


async def check_the_installation(settings: Settings) -> str:
    """The start-up check: record the posture at the first start, then refuse what ADR 0228 refuses.

    Returns the recorded posture. Raises `InstallationRefusedError`, naming the setting or the record that
    refuses, before anything is written: a first start that is refused records nothing."""
    refuse_what_the_settings_forbid(settings)
    wanted = posture_of(settings.env)
    async with session_scope(orgs="*") as session:
        recorded = await recorded_posture(session)
        if recorded is None:
            if wanted == PERSONAL:
                _refuse_more_than_one(await persons_holding_a_membership(session), first_start=True)
            recorded = await record_posture(session, wanted, environment=settings.env)
            logger.info("installation.posture_recorded", posture=recorded, environment=settings.env)
        if recorded != wanted:
            other = "personal" if recorded == PERSONAL else "dev, staging or prod"
            raise InstallationRefusedError(
                f"this database recorded `{recorded}` at its first start and {ENVIRONMENT_VARIABLE}="
                f"{settings.env} is the `{wanted}` posture: the API refuses to start. A posture is recorded "
                f"once and never converted (ADR 0228 §9): serve this database with {ENVIRONMENT_VARIABLE}="
                f"{other}, or start a `{wanted}` installation on a new database."
            )
        if recorded == PERSONAL:
            _refuse_more_than_one(await persons_holding_a_membership(session), first_start=False)
    return recorded


def is_sole_member_refusal(error: BaseException) -> bool:
    """Whether `error` is the database refusing a second person (`choregos_sole_member`)."""
    return isinstance(error, IntegrityError) and SOLE_MEMBER_MARK in str(error.orig)


#: What a refused second person reads, whichever path wrote the membership.
ONE_PERSON_DETAIL = (
    "this installation is personal (ADR 0228): it holds one person, as its `installation` row has recorded "
    "since its first start, and this membership would be a second person's. A machine account (ADR 0131) "
    "is not a person and is accepted; a team needs an organisation installation."
)


def one_person_refusal() -> ApiError:
    """The 409 a second person's membership gets, on every route."""
    return ApiError(
        status.HTTP_409_CONFLICT,
        "A personal installation holds one person",
        ONE_PERSON_DETAIL,
        errors=[{"code": ONE_PERSON, "msg": ONE_PERSON_DETAIL}],
        extra={"code": ONE_PERSON},
    )


def bootstrap_refusal() -> InstallationRefusedError:
    """The bootstrap's own words for the same refusal: its addresses would be a second person."""
    return InstallationRefusedError(
        f"{BOOTSTRAP_ADMINS_VARIABLE} names a person who is not this installation's one person: refused. "
        f"{ONE_PERSON_DETAIL}"
    )


__all__ = [
    "BOOTSTRAP_ADMINS_VARIABLE",
    "ONE_PERSON",
    "ONE_PERSON_DETAIL",
    "ORGANISATION",
    "PERSONAL",
    "SOLE_MEMBER_MARK",
    "InstallationRefusedError",
    "bootstrap_refusal",
    "check_the_installation",
    "is_sole_member_refusal",
    "one_person_refusal",
    "persons_holding_a_membership",
    "posture_of",
    "record_posture",
    "recorded_posture",
    "refuse_what_the_settings_forbid",
]
