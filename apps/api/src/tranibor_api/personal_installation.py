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
  account (`users.is_service`, ADR 0131) is not a person and is accepted; it does not become one while it
  holds a membership and another person holds one (`choregos_sole_member_flip()` on `users`). The API
  answers such a refusal with a 409 whose `code` is `one_person` and whose detail names the record
  (`install_error_handlers`).
- **The start-up check** (`check_the_installation`, from `main.lifespan`, before the bootstrap) refuses to
  serve when `TRANIBOR_ENV` and the recorded posture differ, either way; when more than one person holds a
  membership in the personal posture; when `TRANIBOR_BOOTSTRAP_ADMINS` names more than one address there;
  and on SQLite in that posture, a development database that no migration follows. A first start that
  is refused records nothing.
- **The bootstrap records too** (`record_the_first_start`): `choregos-admin orgs bootstrap` runs it
  without the API's start-up, so it records the posture before its first membership, in the same
  transaction. A database the API never started is governed by the record from its first member on.
- **The sole member decides alone** (ADR 0228 §3, S87-11): `sole_member` is true only when the row says
  `personal` and exactly one person holds a membership. That person, in their own session
  (`decides_alone`), meets what a rule asks of other people: separation of duties, an approver group, a
  human step addressed to a group, an exclusion of ADR 0196 §2 and an approver count above one. Never
  without a fresh authentication (`require_fresh_alone`: the rule's `step_up_minutes`, or 10 minutes when
  it names none), and the decision entry and its audit row say so (`decided_alone`: `sole_member: true`
  and what the rule asked).

Outside the personal posture nothing changes: the row says `organisation`, the trigger lets every
membership through, the start-up only records it, and nobody decides alone.
"""

from __future__ import annotations

from typing import Any

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core.production import ENVIRONMENT_VARIABLE, is_personal

from .config import Settings
from .db.models import Membership, User
from .db.models.installation import ORGANISATION, PERSONAL, SOLE_MEMBER_MARK, Installation
from .db.session import in_platform_scope, session_scope
from .decision_session import holds_a_person_session
from .errors import ApiError
from .freshness import require_fresh_authentication
from .logging import get_logger
from .rbac import Principal

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


async def record_the_first_start(session: AsyncSession, settings: Settings) -> str:
    """Record the posture `TRANIBOR_ENV` names unless one is recorded, and return the recorded one.

    What writes first records: the API's start-up, or the bootstrap run by `choregos-admin orgs bootstrap`
    on a database the API never started, before its first membership, which the record then governs. A
    first personal record is refused, and nothing recorded, when more than one person holds a membership
    already. The session must be in the platform's scope (`orgs="*"`)."""
    recorded = await recorded_posture(session)
    if recorded is not None:
        return recorded
    wanted = posture_of(settings.env)
    if wanted == PERSONAL:
        _refuse_more_than_one(await persons_holding_a_membership(session), first_start=True)
    recorded = await record_posture(session, wanted, environment=settings.env)
    logger.info("installation.posture_recorded", posture=recorded, environment=settings.env)
    return recorded


async def check_the_installation(settings: Settings) -> str:
    """The start-up check: record the posture at the first start, then refuse what ADR 0228 refuses.

    Returns the recorded posture. Raises `InstallationRefusedError`, naming the setting or the record that
    refuses, before anything is written: a first start that is refused records nothing."""
    refuse_what_the_settings_forbid(settings)
    wanted = posture_of(settings.env)
    async with session_scope(orgs="*") as session:
        recorded = await record_the_first_start(session, settings)
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


# ───────────────────────── the sole member decides alone (ADR 0228 §3) ─────────────────────────

#: The decision entry's and the audit row's mark of a decision the sole member took alone.
SOLE_MEMBER = "sole_member"
#: Beside it, what the rule asked of other people: `separation_of_duties`, `group:<name>`, `excluded`,
#: `approvers:<n>` (an action) or `approvals:<n>` (a release).
RULE_ASKED = "rule_asked"
#: How fresh the sole member's authentication must be when the rule names no `step_up_minutes`.
STEP_UP_MINUTES = 10
#: Where a session keeps the posture it has read (`Session.info`).
_POSTURE_READ = "choregos.installation_posture"


async def the_sole_member(session: AsyncSession, org_id: str) -> str | None:
    """The user id of the installation's one person when they decide alone in organisation `org_id`.

    `None` unless the `installation` row says `personal`, exactly one person (not a machine account, ADR
    0131) holds a membership across the installation, and that person holds one in `org_id`. The persons
    are counted in the platform's scope, whatever scope the session is in, and the scope is restored.

    The posture is written once and never changes: it is read once per session, which a list that weighs
    hundreds of requests (`GET /me/pending`) asks about one by one."""
    posture = session.info.get(_POSTURE_READ)
    if posture is None:
        posture = await recorded_posture(session)
        if posture is not None:
            session.info[_POSTURE_READ] = posture
    if posture != PERSONAL:
        return None
    async with in_platform_scope(session):
        rows = (
            await session.execute(
                select(Membership.user_id, Membership.org_id)
                .join(User, User.id == Membership.user_id)
                .where(User.is_service.is_(False))
            )
        ).all()
    persons = {str(user_id) for user_id, _ in rows}
    if len(persons) != 1:
        return None
    if org_id not in {str(org) for _, org in rows}:
        return None
    return persons.pop()


async def sole_member(session: AsyncSession, org_id: str) -> bool:
    """Whether organisation `org_id` is decided by one person alone: the recorded posture is `personal` and
    exactly one person holds a membership (ADR 0228 §3)."""
    return await the_sole_member(session, org_id) is not None


async def decides_alone(session: AsyncSession, org_id: str, principal: Principal) -> bool:
    """Whether `principal` is the sole member of `org_id`, deciding in their own session.

    A decision is still the person's gesture (ADR 0029 §5, ADR 0154 §7): an API token, an MCP client, a
    machine account or a run never decides alone, whatever the posture."""
    if not holds_a_person_session(principal) or principal.authenticated_at is None:
        return False
    return await the_sole_member(session, org_id) == principal.user_id


def require_fresh_alone(principal: Principal, step_up_minutes: int | None, *, gesture: str) -> int:
    """The age of the sole member's authentication, or the usual 401 `step_up_required`.

    Freshness is what stands in for the second person: the rule's `step_up_minutes`, or 10 minutes when it
    names none, whatever the decision and even where the rule would have asked nothing of another person."""
    minutes = STEP_UP_MINUTES if step_up_minutes is None else step_up_minutes
    return require_fresh_authentication(
        principal, minutes, gesture=f"{gesture}, alone as this installation's one person (ADR 0228),"
    )


def decided_alone(rule_asked: list[str]) -> dict[str, Any]:
    """What a decision entry and its audit row carry when the sole member met `rule_asked` alone; nothing
    when no rule asked another person for anything."""
    if not rule_asked:
        return {}
    return {SOLE_MEMBER: True, RULE_ASKED: list(rule_asked)}


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
    "RULE_ASKED",
    "SOLE_MEMBER",
    "SOLE_MEMBER_MARK",
    "STEP_UP_MINUTES",
    "InstallationRefusedError",
    "bootstrap_refusal",
    "check_the_installation",
    "decided_alone",
    "decides_alone",
    "is_sole_member_refusal",
    "one_person_refusal",
    "persons_holding_a_membership",
    "posture_of",
    "record_posture",
    "record_the_first_start",
    "recorded_posture",
    "refuse_what_the_settings_forbid",
    "require_fresh_alone",
    "sole_member",
    "the_sole_member",
]
