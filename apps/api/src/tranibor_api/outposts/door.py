# SPDX-License-Identifier: Apache-2.0
"""The four verbs of the outposts' door: `enrol`, `poll`, `calls/{id}/answer`, `calls/{id}/refuse`.

All `POST`, all JSON, all under `/outposts/v1`, as `packages/contracts/outpost-door.yaml` describes
them. Four is a constraint (ADR 0148 §3): every verb added is one every daemon in the field must
learn. The door serves assignments and never MCP: it lists no tool of the platform, speaks no
JSON-RPC, and has no route that names a connector or a server. The one path parameter is a call's
`{id}`, and a call is met here only as a row (`rendezvous`).

The long poll (ADR 0148 §7). One poll per instance at a time: the slot is on the instance's row,
because a row is what the replicas share, and it lapses by itself should the replica that holds it
die. A second poll while one waits is a 409 `poll_conflict` and raises an alert: two holders of one
key is what a stolen key looks like. While a poll waits it holds no transaction and no pooled
connection, on the model of the hosted sessions' `turns/next`: each look is a short transaction of
its own. It is woken through the relay every replica already runs, and looks again every 5 to 10
seconds whatever it heard. The slot is given back when the poll answers and when its client goes
away: a daemon that restarts must not meet its own dead poll.

The runs lane (door protocol 2, ADR 0226 §3-5) rides in the same poll, for an instance of a person's own
outpost only (`runs.serves`): what the instance holds is reported at the first look, offers are claimed and
cancellations computed at each, and the answer carries `runs` and `cancel_runs`. An organisation's outpost
announces `tools` and nothing else: an enrolment or a poll that announces `runs` there is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import (
    MAX_BODY_BYTES,
    MAX_CONCURRENCY,
    OutpostDoorCode,
    OutpostEnrolmentRequest,
    OutpostInstanceStatus,
    OutpostPollRequest,
    OutpostRefusal,
    OutpostRefusalCode,
    OutpostSealedAnswer,
    OutpostStatus,
)
from tranibor_contracts.outpost import MAX_RUNNING, OUTPOST_ID_PATTERN, PUBLIC_KEY_PATTERN
from tranibor_core import utcnow

from ..audit import record
from ..config import Settings
from ..db.models import Outpost, OutpostInstance
from ..db.session import mark_the_door, release_connection
from ..deps import Config, Db
from ..logging import get_logger
from ..services import outposts as service
from . import auth, rendezvous, runs

router = APIRouter(include_in_schema=False)
logger = get_logger("choregos.outposts.door")

#: How long past the wait a poll asked for its slot lives on: time for its last look. A replica that
#: dies leaves the slot to lapse by then, and the instance's next poll meets a conflict until it does.
SLOT_MARGIN_S: Final = 5
#: A poll with nothing to hand over answers a little sooner than asked: up to this share of the wait.
JITTER: Final = 0.1
#: A second poll under one key raises an alert once in this many seconds for an instance, however
#: many conflicts a stolen key or a daemon in a loop produces.
ALERT_EVERY_S: Final = 60
#: The most ids a poll's answer cancels: what a poll may say it holds (the contract's bound).
MAX_CANCEL: Final = MAX_RUNNING
_ENROLMENT_REFUSALS: Final = {
    # Unknown, already used or expired: the door does not say which.
    OutpostDoorCode.INVALID_ENROLMENT_TOKEN: "the enrolment token is unknown, already used or expired",
    OutpostDoorCode.KEY_ALREADY_ENROLLED: "this signing key is already an instance: a key enrols again "
    "only while it is still pending at the same outpost",
}


async def door_session(session: Db) -> AsyncSession:
    """The request's session, marked as the door's before the verb reads anything (`mark_the_door`).

    A person's runner's rows exist for the door and for no session that merely names no person
    (`<table>_personal_owner`, ADR 0226 §1): the door says what it is, in each of its sessions, whether
    the API serves it or it is served alone (`alone.py` serves this router). The short transactions a
    poll takes for its looks are marked too (`rendezvous.in_scope(..., door=True)`)."""
    await mark_the_door(session)
    return session


#: The session each verb of the door works in.
DoorDb = Annotated[AsyncSession, Depends(door_session)]


def _runs_only_at_a_personal_outpost(outpost: Outpost, capabilities: list[str]) -> None:
    """Refuse an instance of an organisation's outpost that announces `runs`. Protocol 2 lets a personal
    outpost's instance announce it, and no other (ADR 0226 §1, §3): an organisation's outpost announces
    `tools` and nothing else, so an instance that would wait there for an offer is told now, by a 422
    `invalid_request` that says where, rather than left to wait (`outpost-door.yaml`, `x-protocol`,
    `capabilities`)."""
    if service.is_personal(outpost):
        return
    announced = [index for index, capability in enumerate(capabilities) if capability == "runs"]
    if announced:
        raise auth.Refused(
            OutpostDoorCode.INVALID_REQUEST,
            "an organisation's outpost does not take the runs lane: `runs` is announced by an instance of a "
            "person's own outpost only, and an organisation's outpost announces `tools`",
            errors=[
                {
                    "loc": ["capabilities", str(index)],
                    "msg": "`runs` is announced at a person's own outpost only",
                }
                for index in announced
            ],
        )


def _moment(moment: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; PostgreSQL returns UTC ones."""
    return moment if moment is None or moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _served(request: Request, settings: Settings, verb: Awaitable[Response]) -> Response:
    """A verb's answer, or the door's problem for what it refused. A refusal is an answer, not a
    failure: what the request wrote before it (its sequence, an alert) is committed with it."""
    try:
        return await verb
    except auth.Refused as refused:
        return auth.problem(request, settings, refused)


# ───────────────────────── enrol ─────────────────────────


@router.post("/outposts/v1/enrol")
async def enrol(request: Request, session: DoorDb, settings: Config) -> Response:
    """An enrolment token and the instance's public keys; answers a pending instance (ADR 0147 §4)."""
    return await _served(request, settings, _enrol(request, session, settings))


async def _enrol(request: Request, session: AsyncSession, settings: Settings) -> Response:
    raw = await auth.read_body(request, MAX_BODY_BYTES["enrol"])
    document = auth.document_of(raw)
    signing_key = document.get("signing_key") if document is not None else None
    if not isinstance(signing_key, str) or not re.fullmatch(PUBLIC_KEY_PATTERN, signing_key):
        # Before the signature is looked at: there is no key to verify it with.
        raise auth.Refused(OutpostDoorCode.INVALID_REQUEST, "no signing key can be read in the body")
    auth.enrolment_signature(request, settings, raw, signing_key)
    token = document.get("token") if document is not None else None
    try:
        # The token is checked where the instance would be, and is not used up by a request that
        # is refused further down.
        admitted = await service.admit_enrolment(
            session, token if isinstance(token, str) else "", signing_key
        )
        auth.limited(f"enrol:{admitted.outpost.id}", settings)
        auth.speaks(document)
        body = auth.shaped(OutpostEnrolmentRequest, raw)
        # What the verb itself refuses comes after the schema, as the contract orders the checks.
        _runs_only_at_a_personal_outpost(admitted.outpost, list(body.capabilities))
        enrolled = await service.enrol(session, body)
    except service.EnrolmentRefused as refused:
        raise auth.Refused(refused.code, _ENROLMENT_REFUSALS[refused.code]) from None
    return auth.answer(
        settings,
        {
            "instance_id": enrolled.instance.id,
            "organisation": enrolled.organisation,
            "outpost": enrolled.outpost.slug,
            "status": OutpostInstanceStatus.PENDING.value,
            "fingerprint": enrolled.instance.fingerprint,
            "protocol": auth.window(),
            "server_time": auth.server_time(),
        },
        status=201,
    )


# ───────────────────────── poll ─────────────────────────


@router.post("/outposts/v1/poll")
async def poll(request: Request, session: DoorDb, settings: Config) -> Response:
    """Wait for calls to make; learn the cancellations, the door's time and the protocol window."""
    return await _served(request, settings, _poll(request, session, settings))


@dataclass(slots=True)
class _Look:
    """What one look found for a poll."""

    stop: bool = False
    calls: list[dict[str, str]] = field(default_factory=list)
    cancel: list[str] = field(default_factory=list)
    #: The runs lane (protocol 2, a person's own outpost): offers claimed, runs to stop.
    runs: list[dict[str, str]] = field(default_factory=list)
    cancel_runs: list[str] = field(default_factory=list)
    #: The instance or its outpost was revoked since the poll began, or is gone.
    revoked: bool = False
    #: The slot is another poll's: this one overstayed it.
    lost: bool = False

    @property
    def final(self) -> bool:
        return bool(self.calls or self.cancel or self.runs or self.cancel_runs or self.revoked or self.lost)


async def _take_the_slot(
    session: AsyncSession, caller: auth.Caller, body: OutpostPollRequest, holder: str, now: datetime
) -> bool:
    """Take the instance's one poll slot and write what the poll says of the instance, in one
    compare-and-set: of two polls that arrive together, at one replica or at two, one takes it."""
    taken = await session.execute(
        update(OutpostInstance)
        .where(
            OutpostInstance.outpost_id == caller.outpost.id,
            OutpostInstance.id == caller.instance.id,
            or_(
                OutpostInstance.poll_holder.is_(None),
                OutpostInstance.poll_expires_at.is_(None),
                OutpostInstance.poll_expires_at <= now,
            ),
        )
        .values(
            poll_holder=holder,
            poll_expires_at=now + timedelta(seconds=body.wait_s + SLOT_MARGIN_S),
            last_seen_at=now,
            endpoints=list(body.endpoints),
            capabilities=list(body.capabilities),
            daemon_version=body.daemon_version,
            protocol=body.protocol,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(taken.rowcount == 1)  # type: ignore[attr-defined]


async def _give_the_slot_back(outpost_id: str, instance_id: str, holder: str) -> None:
    """Empty the slot if it is still this poll's. A transaction of its own: the poll's request holds none."""
    async with rendezvous.in_scope(outpost_id, door=True) as scope:
        if scope is None:
            return
        await scope.session.execute(
            update(OutpostInstance)
            .where(
                OutpostInstance.outpost_id == outpost_id,
                OutpostInstance.id == instance_id,
                OutpostInstance.poll_holder == holder,
            )
            .values(poll_holder=None, poll_expires_at=None)
            .execution_options(synchronize_session=False)
        )


async def _conflict(session: AsyncSession, caller: auth.Caller, now: datetime) -> auth.Refused:
    """A second poll under one key: the refusal, and the alert. Two holders of one key is what a
    stolen key looks like (ADR 0148 §7).

    The alert is an audit event of the organisation, `outpost.poll_conflict`, under the actor kind
    `outpost`: once in `ALERT_EVERY_S` for an instance, by a compare-and-set on its row, as a budget
    alert is raised once for its threshold. `/metrics` counts those events
    (`choregos_outpost_poll_conflicts_total`), which is what an alert rule reads."""
    raised = await session.execute(
        update(OutpostInstance)
        .where(
            OutpostInstance.outpost_id == caller.outpost.id,
            OutpostInstance.id == caller.instance.id,
            or_(
                OutpostInstance.poll_conflict_at.is_(None),
                OutpostInstance.poll_conflict_at <= now - timedelta(seconds=ALERT_EVERY_S),
            ),
        )
        .values(poll_conflict_at=now)
        .execution_options(synchronize_session=False)
    )
    if raised.rowcount == 1:  # type: ignore[attr-defined]
        await record(
            session,
            caller.principal,
            service.POLL_CONFLICT,
            org_id=caller.outpost.org_id,
            target_type="outpost_instance",
            target_id=caller.instance.id,
            outpost=caller.outpost.slug,
            fingerprint=caller.instance.fingerprint,
        )
        logger.warning(
            "outposts.poll_conflict",
            organisation=caller.organisation,
            outpost=caller.outpost.slug,
            instance=caller.instance.id,
        )
    lapses = _moment(
        (
            await session.execute(
                select(OutpostInstance.poll_expires_at).where(
                    OutpostInstance.outpost_id == caller.outpost.id, OutpostInstance.id == caller.instance.id
                )
            )
        ).scalar_one_or_none()
    )
    wait = max(1, int((lapses - now).total_seconds()) + 1) if lapses is not None else 1
    return auth.Refused(
        OutpostDoorCode.POLL_CONFLICT,
        "another poll is waiting under this instance's key: one poll per instance at a time",
        retry_after=wait,
    )


async def _look(caller: auth.Caller, body: OutpostPollRequest, holder: str, first: bool) -> _Look:
    """One look, in a short transaction of its own under the outpost's scope: is the instance still
    this poll's to serve, is there a call to claim for it, is one of those it holds awaited no more. At a
    person's own outpost polled with protocol 2, the same for its runs lane (`runs`): what the instance
    holds is reported at the first look only, offers are claimed and cancellations read at each."""
    async with rendezvous.in_scope(caller.outpost.id, door=True) as scope:
        if scope is None:
            return _Look(revoked=True)
        session, outpost = scope.session, scope.outpost
        instance = (
            await session.execute(
                select(OutpostInstance)
                .where(OutpostInstance.outpost_id == outpost.id, OutpostInstance.id == caller.instance.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            instance is None
            or instance.status != OutpostInstanceStatus.ACTIVE.value
            or outpost.status == OutpostStatus.REVOKED.value
        ):
            return _Look(revoked=True)
        if instance.poll_holder != holder:
            return _Look(lost=True)
        now = utcnow()
        if first:
            # What ran out of time at this outpost is closed by whoever looks at it.
            await rendezvous.close_what_ran_out(session, outpost.id, now)
        # A paused outpost is handed nothing; what its instances hold may finish (ADR 0148 §10).
        stop = outpost.status != OutpostStatus.ACTIVE.value
        calls = await rendezvous.claim(
            session,
            outpost,
            instance,
            capacity=min(body.capacity, MAX_CONCURRENCY),
            endpoints=list(body.endpoints),
            capabilities=list(body.capabilities),
            now=now,
        )
        cancel = await rendezvous.no_longer_awaited(session, outpost, instance, list(body.running), now)
        found = _Look(stop=stop, calls=calls, cancel=cancel[:MAX_CANCEL])
        if runs.serves(outpost, body):
            if first:
                await runs.report(session, outpost, instance, body, now)
            found.runs = await runs.claim(session, outpost, instance, body.runner, now)
            found.cancel_runs = await runs.no_longer_awaited(
                session, outpost, instance, list(body.runs_running), now
            )
        return found


async def _gone(request: Request) -> None:
    """Returns when the client has gone away. The body was read: nothing else is to come."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _pause(woken: asyncio.Event, gone: asyncio.Task[None], seconds: float) -> None:
    """Until the outpost's calls are announced, the client goes away, or `seconds` have passed."""
    heard = asyncio.ensure_future(woken.wait())
    try:
        await asyncio.wait({heard, gone}, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        heard.cancel()


async def _poll(request: Request, session: AsyncSession, settings: Settings) -> Response:
    raw = await auth.read_body(request, MAX_BODY_BYTES["poll"])
    caller = await auth.authenticate(request, session, settings, raw)
    auth.speaks(auth.document_of(raw))
    body = auth.shaped(OutpostPollRequest, raw)
    _runs_only_at_a_personal_outpost(caller.outpost, list(body.capabilities))
    holder = secrets.token_hex(16)
    now = utcnow()
    if not await _take_the_slot(session, caller, body, holder, now):
        raise await _conflict(session, caller, now)
    found = _Look()
    gone: asyncio.Future[None] | None = None
    # The slot is taken from here: whatever cuts the request short from now on, the commit that
    # makes the slot visible included (a cut during the pool's reset of the connection left it to
    # lapse, and the instance's next polls were refused until it did), gives it back.
    try:
        # From here the request holds nothing: no transaction, no pooled connection (ADR 0148 §7).
        await release_connection(session)
        loop = asyncio.get_running_loop()
        until = loop.time() + body.wait_s * (1 - random.uniform(0, JITTER))  # noqa: S311 - jitter, not a secret
        gone = asyncio.ensure_future(_gone(request))
        with rendezvous.waiters().on(rendezvous.calls_key(caller.outpost.id)) as woken:
            first = True
            while not gone.done():
                woken.clear()
                found = await _look(caller, body, holder, first)
                first = False
                remaining = until - loop.time()
                if found.final or remaining <= 0:
                    break
                await _pause(woken, gone, min(remaining, random.uniform(*rendezvous.LOOK_S)))  # noqa: S311
    finally:
        if gone is not None:
            gone.cancel()
        # Whatever ends the poll, an answer, a client that went away or a request cut short, the slot is
        # given back; shielded, so that a cancellation does not leave it to lapse.
        with contextlib.suppress(Exception):
            await asyncio.shield(_give_the_slot_back(caller.outpost.id, caller.instance.id, holder))
    if found.revoked:
        raise auth.Refused(OutpostDoorCode.INSTANCE_REVOKED, "this instance, or its outpost, was revoked")
    answered: dict[str, Any] = {
        "protocol": auth.window(),
        "server_time": auth.server_time(),
        "stop": found.stop,
        "calls": found.calls,
        "cancel": found.cancel,
    }
    if runs.serves(caller.outpost, body):
        # Protocol 2 at a person's own outpost: the lane's two fields, which protocol 1 never carries.
        answered |= {"runs": found.runs, "cancel_runs": found.cancel_runs}
    return auth.answer(settings, answered)


# ───────────────────────── answer, refuse ─────────────────────────


@router.post("/outposts/v1/calls/{id}/answer")
async def answer(id: str, request: Request, session: DoorDb, settings: Config) -> Response:
    """The sealed answer of a call the instance claimed; recorded once. The door does not open it."""

    def sent(raw: bytes) -> dict[str, Any]:
        return {"answer": auth.shaped(OutpostSealedAnswer, raw).sealed}

    return await _served(request, settings, _recorded(request, session, settings, "answer", id, sent))


@router.post("/outposts/v1/calls/{id}/refuse")
async def refuse(id: str, request: Request, session: DoorDb, settings: Config) -> Response:
    """A refusal code from the contract's closed list; recorded once. It names a class, never a rule."""

    def sent(raw: bytes) -> dict[str, Any]:
        # The list is the contract's model: a code it does not hold is a 422, and one it gains is taken.
        return {"refusal": OutpostRefusalCode(auth.shaped(OutpostRefusal, raw).code)}

    return await _served(request, settings, _recorded(request, session, settings, "refuse", id, sent))


async def _recorded(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    verb: str,
    call_id: str,
    sent: Callable[[bytes], dict[str, Any]],
) -> Response:
    raw = await auth.read_body(request, MAX_BODY_BYTES[verb])
    caller = await auth.authenticate(request, session, settings, raw)
    what = sent(raw)
    try:
        if not re.fullmatch(OUTPOST_ID_PATTERN, call_id):
            raise rendezvous.NoSuchCall(call_id)
        recorded = await rendezvous.record(session, caller.outpost, caller.instance, call_id, **what)
    except rendezvous.NoSuchCall:
        # Another outpost's call, a call that never was, a forged one a daemon refuses: one answer,
        # which does not say whether the call exists (ADR 0148 §6). Never a 403, and no alert.
        raise auth.Refused(OutpostDoorCode.NOT_FOUND, "no such call for this outpost") from None
    except rendezvous.LeaseLost:
        raise auth.Refused(
            OutpostDoorCode.LEASE_LOST,
            "the call is not leased to this instance any more: its lease ended, the outpost's epoch "
            "moved, or the platform stopped waiting for it",
        ) from None
    return auth.answer(settings, {"id": call_id, "recorded": recorded})


__all__ = ["router"]
