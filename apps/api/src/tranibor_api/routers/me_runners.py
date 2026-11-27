# SPDX-License-Identifier: Apache-2.0
"""A person's own runner, as its owner reaches it (ADR 0226 §1-2): `/me/runners`.

A personal runner is a personal outpost, one per person and organisation, whose instances are that
person's machines and which runs that person's runs only. Its owner, and no one else, creates it (on their
first enrolment token), asks for enrolment tokens, approves each machine against the fingerprint its daemon
printed, pauses, resumes and revokes it. The gestures are the management routes' own
(`services.outposts`), on the caller's outpost; the organisation's administrators reach the outpost itself
and nothing of it (`routers/outposts.py`). Another person's runner is a 404 here, as an unknown one is.

Every route answers 404 while `TRANIBOR_PERSONAL_RUNNERS` is off: it is on by default in the personal
posture (ADR 0228) and off elsewhere.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status
from tranibor_contracts.outpost import OUTPOST_ID_PATTERN

from ..audit import record
from ..deps import Db, Me
from ..freshness import authentication_age
from ..schemas.outposts import (
    OutpostEnrolmentCreate,
    OutpostEnrolmentCreated,
    OutpostInstanceApprove,
    PersonalRunnerDto,
    PersonalRunnerInstanceDto,
    PersonalRunnerList,
)
from ..services import outposts as service
from ..services import personal_runners as runners
from ..services.organisations import organisation_by_slug

router = APIRouter(tags=["outposts"])

InstanceId = Annotated[str, Path(pattern=OUTPOST_ID_PATTERN)]


@router.get("/me/runners", response_model=PersonalRunnerList, operation_id="listMyRunners")
async def list_my_runners(
    session: Db, principal: Me, org: Annotated[str | None, Query(max_length=120)] = None
) -> PersonalRunnerList:
    """The caller's runners, one per organisation at most, each with its instances, pending ones first."""
    runners.require_served()
    found = await runners.runners_of(session, principal, org)
    return PersonalRunnerList(
        items=[await runners.view(session, outpost, slug, principal.email) for outpost, slug in found]
    )


@router.post(
    "/me/runners/{org}/enrolments",
    response_model=OutpostEnrolmentCreated,
    status_code=status.HTTP_201_CREATED,
    operation_id="createMyRunnerEnrolment",
)
async def create_my_runner_enrolment(
    org: str, session: Db, principal: Me, body: OutpostEnrolmentCreate | None = None
) -> OutpostEnrolmentCreated:
    """An enrolment token for one of the caller's machines, shown once, creating their runner in this
    organisation the first time (ADR 0226 §1-2)."""
    runners.require_served()
    organisation = await organisation_by_slug(session, org)
    outpost, created = await runners.ensure_runner(session, organisation, principal)
    if created:
        await record(
            session,
            principal,
            "outpost.create",
            org_id=organisation.id,
            target_type="outpost",
            target_id=outpost.id,
            outpost=outpost.slug,
            capabilities=list(outpost.capabilities),
            personal=True,
        )
    issued = await service.create_enrolment(session, outpost, body or OutpostEnrolmentCreate(), principal)
    # Who issued a token, for which runner, until when: never the token.
    await record(
        session,
        principal,
        "outpost.enrol",
        org_id=organisation.id,
        target_type="outpost_enrolment",
        target_id=issued.id,
        outpost=outpost.slug,
        expires_at=issued.expires_at.isoformat(),
    )
    return issued


@router.post(
    "/me/runners/instances/{instance}/approval",
    response_model=PersonalRunnerInstanceDto,
    operation_id="approveMyRunnerInstance",
)
async def approve_my_runner_instance(
    instance: InstanceId, body: OutpostInstanceApprove, session: Db, principal: Me
) -> PersonalRunnerInstanceDto:
    """The owner approves a pending machine against the fingerprint its daemon printed, in a session that
    carries a recent authentication: a token cannot approve (ADR 0147 §4)."""
    runners.require_served()
    outpost = await runners.runner_of_instance(session, principal, instance)
    approved = await service.approve_instance(session, outpost, instance, body.fingerprint, principal)
    await record(
        session,
        principal,
        "outpost.approve",
        org_id=outpost.org_id,
        target_type="outpost_instance",
        target_id=instance,
        outpost=outpost.slug,
        fingerprint=approved.fingerprint,
        auth_age_seconds=authentication_age(principal),
    )
    return runners.instance_view(approved)


@router.post("/me/runners/{org}/pause", response_model=PersonalRunnerDto, operation_id="pauseMyRunner")
async def pause_my_runner(org: str, session: Db, principal: Me) -> PersonalRunnerDto:
    """Nothing more is offered to it; what its instances claimed may finish. Its owner lifts this pause."""
    runners.require_served()
    organisation = await organisation_by_slug(session, org)
    outpost = await runners.my_runner(session, organisation, principal)
    if await service.pause_outpost(session, outpost, service.OWNER):
        await record(
            session,
            principal,
            "outpost.pause",
            org_id=organisation.id,
            target_type="outpost",
            target_id=outpost.id,
            outpost=outpost.slug,
            side=service.OWNER,
        )
    return await runners.view(session, outpost, organisation.slug, principal.email)


@router.post("/me/runners/{org}/resume", response_model=PersonalRunnerDto, operation_id="resumeMyRunner")
async def resume_my_runner(org: str, session: Db, principal: Me) -> PersonalRunnerDto:
    """Lifts the pause its owner made; one an administrator made is theirs to lift (403)."""
    runners.require_served()
    organisation = await organisation_by_slug(session, org)
    outpost = await runners.my_runner(session, organisation, principal)
    if await service.resume_outpost(session, outpost, service.OWNER):
        await record(
            session,
            principal,
            "outpost.resume",
            org_id=organisation.id,
            target_type="outpost",
            target_id=outpost.id,
            outpost=outpost.slug,
            side=service.OWNER,
        )
    return await runners.view(session, outpost, organisation.slug, principal.email)


@router.post("/me/runners/{org}/revocation", response_model=PersonalRunnerDto, operation_id="revokeMyRunner")
async def revoke_my_runner(org: str, session: Db, principal: Me) -> PersonalRunnerDto:
    """Revokes the runner and every instance of it, and moves its epoch (ADR 0147 §5). Final, and nothing
    stands in its way: it answers a machine that may be stolen."""
    runners.require_served()
    organisation = await organisation_by_slug(session, org)
    outpost = await runners.my_runner(session, organisation, principal)
    if await service.revoke_outpost(session, outpost, principal):
        await record(
            session,
            principal,
            "outpost.revoke",
            org_id=organisation.id,
            target_type="outpost",
            target_id=outpost.id,
            outpost=outpost.slug,
            epoch=outpost.epoch,
        )
    return await runners.view(session, outpost, organisation.slug, principal.email)
