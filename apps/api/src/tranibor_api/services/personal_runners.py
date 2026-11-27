# SPDX-License-Identifier: Apache-2.0
"""A person's own runner: a personal outpost, enrolled and approved by its owner only (ADR 0226 §1-2).

A personal runner is an outpost of the organisation that one person owns (`outposts.owner_user_id`), one
per person and organisation (`uq_outposts_personal_owner`), whose instances are that person's machines. It
announces `runs` and nothing else (an organisation's outpost announces `tools`), so no connector routes to
it (`services.outpost_route`). It is created the first time its owner asks for an enrolment token, never
by hand, and named by a reserved slug, `personal-<12 hex of the user id>`, which no organisation's outpost
may take.

Its owner reaches it through `/me/runners` (`routers/me_runners.py`): the enrolment token, the approval
against the fingerprint, pause, resume and revocation, each the management routes' own gesture
(`services.outposts`) on the owner's outpost. Every other person meets a 404 there: the outpost is looked
up among the caller's own, and its instances, enrolment tokens and calls are under a restrictive
row-level policy that shows them to no other person (`20261127_1200_s87_15_personal_outposts`). The
organisation's administrators reach the outpost itself and nothing of it (`refuse_personal`).

What a runner announces of its runs lane (its modes, the providers whose sign-in it holds, its capacity,
the offers it runs and those that wait for it) is recorded from door protocol 2 (S87-16): until then each
reads empty.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import OutpostCapability, OutpostStatus

from ..config import get_settings
from ..db.models import Organization, Outpost
from ..db.session import in_outpost_scope
from ..errors import ApiError, not_found
from ..rbac import Principal
from ..schemas.outposts import OutpostInstanceDto, PersonalRunnerDto, PersonalRunnerInstanceDto
from . import outposts as service


def _aware(moment: datetime) -> datetime:
    """SQLite returns naive datetimes; PostgreSQL returns UTC ones."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def runner_slug(user_id: str) -> str:
    """The reserved slug of a person's runner: stable, derived from the user id, never their address."""
    return service.PERSONAL_PREFIX + hashlib.sha256(user_id.encode()).hexdigest()[:12]


def require_served() -> None:
    """`/me/runners` answers 404 while personal runners are off (`TRANIBOR_PERSONAL_RUNNERS`, on by
    default in the personal posture only)."""
    if not get_settings().personal_runners_on:
        raise ApiError(
            404,
            "Not Found",
            "personal runners are not served on this deployment (`TRANIBOR_PERSONAL_RUNNERS`)",
        )


def require_a_person(principal: Principal) -> None:
    """A runner is a person's: a machine account (ADR 0131) owns none, and creates none."""
    if principal.kind != "user":
        raise ApiError(404, "Not Found", "a machine account has no personal runner: a runner is a person's")


async def runners_of(
    session: AsyncSession, principal: Principal, org: str | None = None
) -> list[tuple[Outpost, str]]:
    """The caller's runners, with their organisation's slug, among the organisations the session sees:
    one per organisation at most."""
    query = (
        select(Outpost, Organization.slug)
        .join(Organization, Organization.id == Outpost.org_id)
        .where(Outpost.owner_user_id == principal.user_id)
        .order_by(Organization.slug)
    )
    if org is not None:
        query = query.where(Organization.slug == org)
    async with in_outpost_scope(session, None):  # the registry, every outpost of the scope
        rows = (await session.execute(query)).all()
    return [(row[0], str(row[1])) for row in rows]


async def runner_in(
    session: AsyncSession, organisation: Organization, principal: Principal
) -> Outpost | None:
    """The caller's runner in the organisation, if they have one."""
    return (
        await session.execute(
            select(Outpost).where(
                Outpost.org_id == organisation.id, Outpost.owner_user_id == principal.user_id
            )
        )
    ).scalar_one_or_none()


async def my_runner(session: AsyncSession, organisation: Organization, principal: Principal) -> Outpost:
    """The caller's runner in the organisation, or the 404 that says they have none."""
    require_a_person(principal)
    outpost = await runner_in(session, organisation, principal)
    if outpost is None:
        raise not_found("Personal runner", organisation.slug)
    return outpost


async def runner_of_instance(session: AsyncSession, principal: Principal, instance_id: str) -> Outpost:
    """The caller's runner the instance belongs to, read from the id the instance carries; the 404 of an
    unknown instance for an instance of another person's runner, or of an organisation's outpost."""
    unknown = not_found("Outpost instance", instance_id)
    require_a_person(principal)
    outpost_id = service.outpost_id_of_instance(instance_id)
    if outpost_id is None:
        raise unknown
    outpost = (
        await session.execute(
            select(Outpost).where(Outpost.id == outpost_id, Outpost.owner_user_id == principal.user_id)
        )
    ).scalar_one_or_none()
    if outpost is None:
        raise unknown
    return outpost


async def ensure_runner(
    session: AsyncSession, organisation: Organization, principal: Principal
) -> tuple[Outpost, bool]:
    """The caller's runner in the organisation, created on first use (ADR 0226 §1), and whether it was
    created now. Nothing is created where no enrolment token could be issued (outposts off, no door key).
    Idempotent, and safe when two requests of the same person race: the unique index keeps one, and the
    loser reads it back."""
    require_a_person(principal)
    service.require_enrolments()
    existing = await runner_in(session, organisation, principal)
    if existing is not None:
        return existing, False
    slug = runner_slug(principal.user_id)
    outpost = Outpost(
        org_id=organisation.id,
        slug=slug,
        display_name=f"Personal runner of {principal.display_name or principal.email}"[:200],
        status=OutpostStatus.ACTIVE.value,
        capabilities=[OutpostCapability.RUNS.value],
        epoch=0,
        created_by=principal.email,
        owner_user_id=principal.user_id,
    )
    try:
        async with session.begin_nested():
            session.add(outpost)
            await session.flush()
    except IntegrityError:
        raced = await runner_in(session, organisation, principal)
        if raced is None:  # the slug is taken by an outpost that is not this person's
            raise service.refusal(
                "outpost_exists", f"`{slug}` is already an outpost of {organisation.slug}", slug
            ) from None
        return raced, False
    return outpost, True


def instance_view(instance: OutpostInstanceDto) -> PersonalRunnerInstanceDto:
    """One of the owner's machines, as `/me/runners` shows it."""
    return PersonalRunnerInstanceDto(
        id=instance.id,
        status=instance.status,
        fingerprint=instance.fingerprint,
        daemon_version=instance.daemon_version,
        protocol=instance.protocol,
        # Announced in a poll of door protocol 2, which is S87-16's: nothing to read before it.
        modes=[],
        providers=[],
        capacity=0,
        runs_running=0,
        facts=dict(instance.facts),
        enrolled_at=instance.enrolled_at,
        approval_step_up_minutes=instance.approval_step_up_minutes,
        approved_at=instance.approved_at,
        last_seen_at=instance.last_seen_at,
        online=instance.online,
    )


async def view(session: AsyncSession, outpost: Outpost, org: str, owner: str) -> PersonalRunnerDto:
    """The runner as its owner sees it, with its instances, pending ones first."""
    instances = await service.list_instances(session, outpost)
    seen = [i.last_seen_at for i in instances if i.last_seen_at is not None]
    return PersonalRunnerDto(
        org=org,
        slug=outpost.slug,
        owner=owner,
        status=OutpostStatus(outpost.status),
        paused_by=service.paused_by(outpost),
        online=any(i.online for i in instances),
        last_seen_at=max(seen) if seen else None,
        instances=[instance_view(i) for i in instances],
        # The offers that wait for it are S87-16's table; none exists before it.
        runs_waiting=0,
        created_at=_aware(outpost.created_at),
    )


__all__ = [
    "ensure_runner",
    "instance_view",
    "my_runner",
    "require_a_person",
    "require_served",
    "runner_in",
    "runner_of_instance",
    "runner_slug",
    "runners_of",
    "view",
]
