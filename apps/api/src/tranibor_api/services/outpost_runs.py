# SPDX-License-Identifier: Apache-2.0
"""A run offered to a person's runner: the platform's side of the door's runs lane (ADR 0226 §4-6).

A run placed on a person's runner is a row of `outpost_runs`, an offer, under the run's id (or a
conversation turn's). Two sides meet on it, as a call's two sides meet on a row of `outpost_calls`:

- **the platform** places the offer (`offer_run`), reads it (`status`) and withdraws it (`withdraw`); the
  outpost executor (S87-17) calls these three, from the orchestrator, under the platform's scope;
- **the door** claims it for the instance that polls, mints the run token, seals it and signs it, renews its
  lease and records its end (`outposts/runs.py`).

The life of an offer, in `state`: `offered` until one instance claims it (`claimed`), which it ends
(`ended`, with an exit code); `lost` when its lease lapses or its instance is revoked; `cancelled` when the
platform withdraws it; `expired` when nobody claimed it within its queue bound. Each move is a
compare-and-set on the state it leaves.

A lost offer's run is closed through ADR 0143's closure (`tranibor_core.run_closure.close_run`, the reason
`lost`), here and nowhere else (`close_lost`): by the platform's sweep (`rendezvous.sweep`), by `status`, and
by a revocation, each under a scope that sees the run's row. The door marks an offer lost and does not close
the run: it works under one organisation's scope and names no person (ADR 0148 §6), so the run's row in a
personal space is not visible to it. From the close on, the run's API answers 409 `run_closed`
(ADR 0143 §5); the claimed process can still call in between, the same window any executor's lost run has
(ADR 0226 §5).

Row-level security (ADR 0148 §6, ADR 0226 §1). Every statement here names the offer's outpost
(`in_outpost_scope`); a personal outpost's offers exist only under the platform's scope, for the door, and
for a session of their owner (`outpost_runs_personal_owner`).

This module is imported by the door: it imports nothing that reaches an organisation's connectors
(ADR 0148 §12), and the rendezvous only when it wakes a poll.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import OutpostStatus, StageResult
from tranibor_contracts.enums import OfferKind, RunCredential, SubscriptionProvider
from tranibor_contracts.outpost import MAX_SEALED_RUN_BYTES, RunIsolation
from tranibor_contracts.outpost import OutpostRun as OfferPlaintext
from tranibor_core import utcnow
from tranibor_core.redaction import redact
from tranibor_core.run_closure import OPEN_RUN_STATUSES, close_run
from tranibor_core.subscription_credentials import SUBSCRIPTION_VARIABLES, shapes_in

from ..db.models import Outpost, OutpostRun, Run
from ..db.session import in_outpost_scope
from ..logging import get_logger

logger = get_logger("choregos.outposts.runs")


class OutpostRunState(StrEnum):
    """The life of an offer of the runs lane (ADR 0226 §4-6)."""

    OFFERED = "offered"
    CLAIMED = "claimed"
    ENDED = "ended"
    LOST = "lost"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


OFFERED, CLAIMED, ENDED = OutpostRunState.OFFERED, OutpostRunState.CLAIMED, OutpostRunState.ENDED
LOST, CANCELLED, EXPIRED = OutpostRunState.LOST, OutpostRunState.CANCELLED, OutpostRunState.EXPIRED

#: An offer's lease when its placement names none: four polls of the longest wait, and a margin.
DEFAULT_LEASE_S: Final = 120
#: How long an offer waits for a claim when its placement names no bound: the orchestrator's queue bound
#: (`QUEUE_MAX_MINUTES`).
DEFAULT_QUEUE_MINUTES: Final = 360
#: What a run token minted at the claim adds to the offer's maximum minutes (ADR 0226 §4, §11).
TOKEN_MARGIN_MINUTES: Final[dict[OfferKind, int]] = {OfferKind.STAGE_RUN: 15, OfferKind.CONVERSATION_TURN: 5}
#: The reason a run whose offer was lost is closed with (ADR 0143 §2).
LOST_REASON: Final = "lost"
#: Room kept for the run token when an offer is weighed before it has one: an ES256 token of the
#: platform is about 600 bytes.
TOKEN_ROOM: Final = 2048
#: A run token's shape, for the check of an offer's plaintext before any token exists.
_NO_TOKEN_YET: Final = "x.y.z"  # noqa: S105 - a shape, not a token

#: Why `offer_run` wrote nothing.
NOT_PERSONAL: Final = "not_personal"
REVOKED: Final = "revoked"
NOT_SUBSCRIPTION: Final = "not_subscription"
INVALID: Final = "invalid"
SECRET: Final = "secret"  # noqa: S105 - a reason, not a secret
TOO_LARGE: Final = "too_large"
ID_TAKEN: Final = "id_taken"


class NotOffered(Exception):  # noqa: N818 - a decision at placement, not a failure
    """An offer the platform does not place: nothing was written, and no runner will see it. `reason` is
    one of the constants above."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Offer:
    """An offer as the platform reads it: what the outpost executor maps to a run's status (S87-17)."""

    id: str
    outpost_id: str
    kind: OfferKind
    state: OutpostRunState
    #: The instance that claimed it, and when; until when its lease runs.
    instance_id: str | None
    claimed_at: datetime | None
    lease_until: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    expires_at: datetime
    #: False when a row already answered for this id: a retried placement met the first offer.
    created: bool = False


def _aware(moment: datetime) -> datetime:
    """SQLite returns naive datetimes; PostgreSQL returns UTC ones."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _when(moment: datetime | None) -> datetime | None:
    return None if moment is None else _aware(moment)


def _of(row: OutpostRun, *, created: bool = False) -> Offer:
    return Offer(
        id=row.id,
        outpost_id=row.outpost_id,
        kind=OfferKind(row.kind),
        state=OutpostRunState(row.state),
        instance_id=row.instance_id,
        claimed_at=_when(row.claimed_at),
        lease_until=_when(row.lease_until),
        finished_at=_when(row.finished_at),
        exit_code=row.exit_code,
        expires_at=_aware(row.expires_at),
        created=created,
    )


def plaintext(row: OutpostRun, run_token: str) -> dict[str, Any]:
    """What the claiming instance reads once it has opened the envelope: `outpost-run.schema.json`. The
    offer's id is its run's for a stage's run, its turn's for a conversation turn."""
    turn = row.kind == OfferKind.CONVERSATION_TURN.value
    return {
        "id": row.id,
        "kind": row.kind,
        "run_id": None if turn else row.id,
        "conversation_id": row.conversation_id if turn else None,
        "turn_id": row.id if turn else None,
        "api_url": row.api_url,
        "run_token": run_token,
        "env": dict(row.env or {}),
        "credential": row.credential,
        "provider": row.provider,
        "isolation": row.isolation,
        "lease_s": row.lease_s,
        "max_minutes": row.max_minutes,
    }


def would_weigh(row: OutpostRun) -> int:
    """What the offer will weigh as a poll's answer carries it, before it has a token or a claimer: the
    envelope is the plaintext with 60 bytes around it, in base64url; its id, its kind, the signature and
    the element's punctuation are under a kilobyte more. The claim weighs the real thing."""
    plain = len(json.dumps(plaintext(row, "x" * TOKEN_ROOM)).encode("utf-8"))
    return len(row.id) + len(row.kind) + 4 * ((plain + 60 + 2) // 3) + 1024


def _refuse_secrets(env: Mapping[str, str]) -> None:
    """An offer carries no secret of the platform's and no subscription's credential (ADR 0226 §9,
    ADR 0227 §4): a value that holds a subscription's shape, a variable that holds one by name, or a
    value the log redaction would mask is refused. The refusal names the variable, never its value."""
    for name, value in env.items():
        assignment = f"{name}={value}"
        if name in SUBSCRIPTION_VARIABLES or shapes_in(value) or redact(assignment) != assignment:
            raise NotOffered(
                SECRET,
                f"the offer's environment carries a secret under `{name}`: an offer is read by a machine the "
                "platform verifies nothing on, and carries names and addresses only",
            )


async def _announce(session: AsyncSession, outpost_id: str) -> None:
    """Wake the polls that wait at this outpost, once this transaction has committed."""
    from ..outposts import rendezvous  # the door's module imports this one

    await rendezvous.announce(session, rendezvous.calls_key(outpost_id))


async def _row(session: AsyncSession, outpost_id: str, offer_id: str) -> OutpostRun | None:
    """The offer as the database holds it now. The session names the outpost."""
    return (
        await session.execute(
            select(OutpostRun)
            .where(OutpostRun.outpost_id == outpost_id, OutpostRun.id == offer_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ───────────────────────── placing, reading, withdrawing an offer ─────────────────────────


async def offer_run(  # noqa: PLR0913 - one offer is these facts, keyword-only
    session: AsyncSession,
    outpost: Outpost,
    *,
    offer_id: str,
    project_slug: str,
    work_item_key: str,
    api_url: str,
    credential: RunCredential,
    isolation: RunIsolation,
    max_minutes: int,
    provider: SubscriptionProvider | None = None,
    kind: OfferKind = OfferKind.STAGE_RUN,
    project_id: str | None = None,
    conversation_id: str | None = None,
    token_subject: str | None = None,
    env: Mapping[str, str] | None = None,
    lease_s: int = DEFAULT_LEASE_S,
    queue_minutes: int = DEFAULT_QUEUE_MINUTES,
    now: datetime | None = None,
) -> Offer:
    """Offer a run to a person's runner, and wake its polls. Nothing is minted here: the run token is the
    claim's (ADR 0226 §4).

    Replayable (ADR 0008): an offer is under its run's id, and placing it again meets the first row
    (`Offer.created` is false), whatever became of it. `NotOffered` when the outpost is an organisation's or
    revoked, when the offer is not a subscription run (an offer carries no gateway key in phase A,
    ADR 0226 §9), when its plaintext is not the contract's, when its environment carries a secret, when it
    would weigh more than the door carries, or when its id is another offer's."""
    if outpost.owner_user_id is None:
        raise NotOffered(NOT_PERSONAL, f"`{outpost.slug}` is an organisation's outpost: it takes no run")
    if outpost.status == OutpostStatus.REVOKED.value:
        raise NotOffered(REVOKED, f"`{outpost.slug}` is revoked: no machine of it will claim a run")
    if RunCredential(credential) is not RunCredential.SUBSCRIPTION:
        raise NotOffered(
            NOT_SUBSCRIPTION,
            "a person's runner takes subscription runs only: an offer carries no gateway key (ADR 0226 §9)",
        )
    kind = OfferKind(kind)
    subject = token_subject or (offer_id if kind is OfferKind.STAGE_RUN else None)
    if subject is None:
        raise NotOffered(INVALID, "a conversation turn's offer names the subject of its token (`turn-…`)")
    if queue_minutes < 1:
        raise NotOffered(INVALID, "an offer waits one minute at least")
    environment = {str(name): str(value) for name, value in (env or {}).items()}
    _refuse_secrets(environment)
    now = now or utcnow()
    row = OutpostRun(
        id=offer_id,
        org_id=outpost.org_id,
        outpost_id=outpost.id,
        project_id=project_id,
        kind=kind.value,
        state=OFFERED.value,
        credential=RunCredential.SUBSCRIPTION.value,
        provider=SubscriptionProvider(provider).value if provider is not None else None,
        isolation=isolation,
        env=environment,
        api_url=api_url,
        token_subject=subject,
        project_slug=project_slug,
        work_item_key=work_item_key,
        conversation_id=conversation_id,
        max_minutes=max_minutes,
        lease_s=lease_s,
        expires_at=now + timedelta(minutes=queue_minutes),
    )
    try:
        OfferPlaintext.model_validate(plaintext(row, _NO_TOKEN_YET))
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "offer"
        raise NotOffered(
            INVALID, f"the offer is not the contract's `OutpostRun`: {where}: {first['msg']}"
        ) from None
    if would_weigh(row) > MAX_SEALED_RUN_BYTES:
        raise NotOffered(
            TOO_LARGE, f"the offer would weigh more than the {MAX_SEALED_RUN_BYTES} bytes the door carries"
        )
    taken = NotOffered(ID_TAKEN, f"`{offer_id}` is already another offer")
    async with in_outpost_scope(session, outpost.id):
        existing = await _row(session, outpost.id, offer_id)
        if existing is None:
            try:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                # Placed at the same moment by a replay of the same activity, or the id of another
                # outpost's offer, which this session does not see.
                existing = await _row(session, outpost.id, offer_id)
                if existing is None:
                    raise taken from None
            else:
                await _announce(session, outpost.id)
                logger.info("outpost_runs.offered", offer=offer_id, outpost=outpost.id, kind=kind.value)
                return _of(row, created=True)
    if existing.kind != kind.value:
        raise taken
    return _of(existing)


async def status(
    session: AsyncSession, outpost_id: str, offer_id: str, *, now: datetime | None = None
) -> Offer | None:
    """The offer as it stands now, `None` when there is none. A lease that lapsed or a queue bound that
    passed is taken into account here, whoever looks first, and a lost offer's run is closed (`close_lost`)
    when the session sees the run's row, as the orchestrator's does."""
    now = now or utcnow()
    async with in_outpost_scope(session, outpost_id):
        await lapse(session, outpost_id, now, offer_id=offer_id)
        await close_lost(session, outpost_id, now, offer_id=offer_id)
        row = await _row(session, outpost_id, offer_id)
        return _of(row) if row is not None else None


async def withdraw(
    session: AsyncSession, outpost_id: str, offer_id: str, *, now: datetime | None = None
) -> OutpostRunState | None:
    """The platform stops waiting for an offer: `cancelled`, while it was `offered` or `claimed`. A claimed
    one is carried in `cancel_runs` to the instance that runs it, at its next poll, which is woken. Returns
    the state the offer is left in, `None` when there is none; an offer that already ended, was lost or
    expired stays what it is."""
    now = now or utcnow()
    async with in_outpost_scope(session, outpost_id):
        moved = await session.execute(
            update(OutpostRun)
            .where(
                OutpostRun.outpost_id == outpost_id,
                OutpostRun.id == offer_id,
                OutpostRun.state.in_([OFFERED.value, CLAIMED.value]),
            )
            .values(state=CANCELLED.value, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        if moved.rowcount == 1:  # type: ignore[attr-defined]
            await _announce(session, outpost_id)
        state = (
            await session.execute(
                select(OutpostRun.state).where(OutpostRun.outpost_id == outpost_id, OutpostRun.id == offer_id)
            )
        ).scalar_one_or_none()
    return OutpostRunState(state) if state is not None else None


@dataclass(frozen=True, slots=True)
class Load:
    """What waits for a person's runner and what its machines run, as its owner's list shows it."""

    #: Offers nobody claimed yet, within their queue bound.
    waiting: int
    #: Claimed offers, by the instance that holds them.
    running: dict[str, int]


async def load(session: AsyncSession, outpost_id: str, *, now: datetime | None = None) -> Load:
    """Count the offers that wait for the outpost and those each of its instances runs. The session names
    no outpost or this one, and sees the outpost's offers (its owner's, or the platform's scope)."""
    now = now or utcnow()
    async with in_outpost_scope(session, outpost_id):
        rows = await session.execute(
            select(OutpostRun.state, OutpostRun.instance_id, func.count())
            .where(
                OutpostRun.outpost_id == outpost_id,
                (OutpostRun.state == CLAIMED.value)
                | ((OutpostRun.state == OFFERED.value) & (OutpostRun.expires_at > now)),
            )
            .group_by(OutpostRun.state, OutpostRun.instance_id)
        )
        counted = rows.all()
    waiting = sum(int(count) for state, _, count in counted if state == OFFERED.value)
    running = {
        str(holder): int(count) for state, holder, count in counted if state == CLAIMED.value and holder
    }
    return Load(waiting=waiting, running=running)


# ───────────────────────── leases that lapse, runs that close ─────────────────────────


async def lapse(session: AsyncSession, outpost_id: str, now: datetime, *, offer_id: str | None = None) -> int:
    """Close what ran out of time at an outpost: a claimed offer past its lease is `lost`, an offer nobody
    claimed past its queue bound is `expired`. Each is a compare-and-set on the state it leaves. The session
    names the outpost. Returns how many offers moved."""
    moved = 0
    for was, becomes, clock in (
        (CLAIMED, LOST, OutpostRun.lease_until),
        (OFFERED, EXPIRED, OutpostRun.expires_at),
    ):
        conditions = [OutpostRun.outpost_id == outpost_id, OutpostRun.state == was.value, clock <= now]
        if offer_id is not None:
            conditions.append(OutpostRun.id == offer_id)
        done = await session.execute(
            update(OutpostRun)
            .where(*conditions)
            .values(state=becomes.value, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        moved += int(done.rowcount or 0)  # type: ignore[attr-defined]
    return moved


async def lose_held(
    session: AsyncSession, outpost_id: str, *, instance_id: str | None = None, now: datetime | None = None
) -> int:
    """A revocation loses the offers its instances held, at once (ADR 0226 §5): those of one instance, or of
    every instance of the outpost. The session names the outpost. Returns how many were lost."""
    conditions = [OutpostRun.outpost_id == outpost_id, OutpostRun.state == CLAIMED.value]
    if instance_id is not None:
        conditions.append(OutpostRun.instance_id == instance_id)
    done = await session.execute(
        update(OutpostRun)
        .where(*conditions)
        .values(state=LOST.value, finished_at=now or utcnow())
        .execution_options(synchronize_session=False)
    )
    return int(done.rowcount or 0)  # type: ignore[attr-defined]


async def close_lost(
    session: AsyncSession, outpost_id: str, now: datetime | None = None, *, offer_id: str | None = None
) -> int:
    """Close the runs of the outpost's lost offers that are still open, through ADR 0143's closure: status
    `failed`, the reason `lost`, the end date, written together, under the lock an update takes, and never
    over a result already there (`close_run`: the runner's own result, posted first, stays). The session
    names the outpost and must see the runs' rows: the platform's scope, or the run's owner's; a run it does
    not see is left for the sweep. Returns how many runs this call closed."""
    query = (
        select(OutpostRun.id)
        .join(Run, Run.id == OutpostRun.id)
        .where(
            OutpostRun.outpost_id == outpost_id,
            OutpostRun.state == LOST.value,
            OutpostRun.kind == OfferKind.STAGE_RUN.value,
            Run.status.in_(sorted(OPEN_RUN_STATUSES)),
        )
    )
    if offer_id is not None:
        query = query.where(OutpostRun.id == offer_id)
    closed = 0
    for run_id in list((await session.execute(query)).scalars()):
        run = await session.get(Run, run_id, populate_existing=True, with_for_update={"key_share": True})
        lost = StageResult.failure(
            "the person's runner that claimed this run lost it: its lease lapsed, or its machine was revoked",
            LOST_REASON,
        )
        if run is not None and close_run(run, lost, status="failed", at=now):
            closed += 1
            logger.info("outpost_runs.run_closed", run_id=run_id, outpost=outpost_id, reason=LOST_REASON)
    await session.flush()
    return closed


async def sweep_outpost(session: AsyncSession, outpost_id: str, now: datetime) -> int:
    """What the platform's sweep does at one outpost (`rendezvous.sweep`), under its scope: lapse what ran
    out of time, then close the runs of the lost offers. Returns how many offers moved."""
    moved = await lapse(session, outpost_id, now)
    await close_lost(session, outpost_id, now)
    return moved


__all__ = [
    "DEFAULT_LEASE_S",
    "DEFAULT_QUEUE_MINUTES",
    "LOST_REASON",
    "TOKEN_MARGIN_MINUTES",
    "Load",
    "NotOffered",
    "Offer",
    "OutpostRunState",
    "close_lost",
    "lapse",
    "load",
    "lose_held",
    "offer_run",
    "plaintext",
    "status",
    "sweep_outpost",
    "withdraw",
    "would_weigh",
]
