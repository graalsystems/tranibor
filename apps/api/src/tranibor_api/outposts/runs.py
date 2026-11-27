# SPDX-License-Identifier: Apache-2.0
"""The runs lane of the outposts' door: an offer claimed once, the run token minted at the claim (door
protocol 2, ADR 0226 §3-5).

The lane lives inside the poll, with no verb of its own (ADR 0148 §3). It is served to an instance of a
**personal** outpost that polls with protocol 2 (`serves`); an organisation's outpost announces `tools` and
nothing else, and a daemon of protocol 1 is handed nothing of it. Each poll:

- **reports** what its instance holds, at its first look (`report`): a lease that lapsed is lost first
  (`services.outpost_runs.lapse`), then each offer the instance lists in `runs_running` has its lease set to
  `lease_s` from this poll, never past the claim plus the run's maximum minutes, and each offer in
  `runs_ended` takes its exit code. A poll of another instance renews nothing, and nothing else extends a
  lease: the lifetime is the door's, whatever the daemon asks for (ADR 0148 §9).
- **claims** offers for it, at each look (`claim`), when it announces `runs` and says what it takes
  (`runner`): only an offer of a kind it knows, an isolation of a mode it runs and, for a subscription run, a
  provider whose sign-in it holds, at most `run_capacity`, the oldest first, with `FOR UPDATE SKIP LOCKED`
  and a compare-and-set on a row no instance ever claimed: of two claimers one wins, and a claimed offer is
  never handed over again, to any instance. Then, and never before, the door mints the run token
  (`mint_run_token`, valid the run's maximum minutes plus 15, a turn's plus 5), seals it with the assignment
  to the claiming instance's key under the offer's id (`sealing.seal`), and signs the envelope
  (`rendezvous.sign_run`, `x-run-signature`). The row never holds the token: it exists in the poll's answer
  only.
- **cancels**, at each look (`no_longer_awaited`): the offers the instance lists that the door waits for no
  more (lost, withdrawn, ended, another instance's, unknown), and once each offer it held that was lost or
  withdrawn since, whether it lists it or not.

The door marks an offer lost; it does not close the run, whose row in a personal space it cannot see: the
platform does, through ADR 0143's closure (`services.outpost_runs.close_lost`).

Every statement runs in the poll's short transaction under the outpost's scope, marked as the door's
(`rendezvous.in_scope(..., door=True)`): a personal outpost's offers exist for the door by that mark, never
for a session that merely names no person (`outpost_runs_personal_owner`). This module is part of the door's
package: it imports nothing that reaches an organisation's connectors (ADR 0148 §12).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_adapters import sealing
from tranibor_contracts import OutpostCapability, OutpostInstanceStatus, OutpostPollRequest, OutpostStatus
from tranibor_contracts.enums import OfferKind, RunCredential
from tranibor_contracts.outpost import (
    MAX_RUN_CAPACITY,
    MAX_RUNS_RUNNING,
    MAX_SEALED_RUN_BYTES,
    RUNS_PROTOCOL,
    OutpostRunEnded,
    OutpostRunner,
)
from tranibor_core.production import is_production

from ..config import Settings, get_settings
from ..db.models import Outpost, OutpostInstance, OutpostRun
from ..logging import get_logger
from ..security import mint_run_token
from ..services import outpost_runs as offers
from . import rendezvous

logger = get_logger("choregos.outposts.runs")

#: The isolation each mode of a runner runs an agent in (ADR 0226 §10).
ISOLATION_OF_MODE: Final[dict[str, str]] = {"docker": "container", "native": "process"}
_RUNS = OutpostCapability.RUNS.value
_ACTIVE = OutpostStatus.ACTIVE.value


def _aware(moment: datetime) -> datetime:
    """SQLite returns naive datetimes; PostgreSQL returns UTC ones."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def serves(outpost: Outpost, body: OutpostPollRequest) -> bool:
    """Whether this poll's runs lane is served: a poll of protocol 2 at a person's own outpost. An
    organisation's outpost's poll is answered as protocol 1 answered it, whatever it says of the lane."""
    return body.protocol >= RUNS_PROTOCOL and outpost.owner_user_id is not None


def check_the_runs_lane(settings: Settings) -> None:
    """The door mints run tokens when personal runners are on (ADR 0226 §4), so the process that serves it
    holds the run-token key the API verifies them with. Without it, a door served alone would sign with a
    key of its own that no API knows, and every run a person's machine starts would be refused: refused at
    start-up in staging, prod and the personal posture, naming the setting."""
    if settings.personal_runners_on and not settings.run_token_private_key and is_production(settings.env):
        raise RuntimeError(
            "personal runners are on (`TRANIBOR_PERSONAL_RUNNERS`) and TRANIBOR_RUN_TOKEN_PRIVATE_KEY is not "
            "set: the outposts' door mints the run token of each run a person's machine claims, with the key "
            "the API verifies it with. Give the door that key, or switch personal runners off."
        )


# ───────────────────────── what a poll reports ─────────────────────────


async def report(
    session: AsyncSession,
    outpost: Outpost,
    instance: OutpostInstance,
    body: OutpostPollRequest,
    now: datetime,
) -> None:
    """What the poll says of the offers its instance holds, taken once, at its first look: what ran out of
    time is lost first, then the listed offers are renewed and the ended ones recorded."""
    await offers.lapse(session, outpost.id, now)
    await _renew(session, outpost, instance, list(dict.fromkeys(body.runs_running)), now)
    await _ended(session, outpost, instance, list(body.runs_ended), now)


async def _renew(
    session: AsyncSession, outpost: Outpost, instance: OutpostInstance, running: list[str], now: datetime
) -> None:
    """Each listed offer this instance holds, under a lease still running, gets `lease_s` from now, never
    past its claim plus its maximum minutes. A lease that lapsed is not revived: the offer is lost."""
    if not running:
        return
    held = await session.execute(
        select(OutpostRun.id, OutpostRun.claimed_at, OutpostRun.max_minutes, OutpostRun.lease_s).where(
            OutpostRun.outpost_id == outpost.id,
            OutpostRun.id.in_(running),
            OutpostRun.state == offers.CLAIMED.value,
            OutpostRun.instance_id == instance.id,
            OutpostRun.lease_until > now,
        )
    )
    for offer_id, claimed_at, max_minutes, lease_s in held.all():
        until = now + timedelta(seconds=lease_s)
        if claimed_at is not None:
            until = min(until, _aware(claimed_at) + timedelta(minutes=max_minutes))
        await session.execute(
            update(OutpostRun)
            .where(
                OutpostRun.outpost_id == outpost.id,
                OutpostRun.id == offer_id,
                OutpostRun.state == offers.CLAIMED.value,
                OutpostRun.instance_id == instance.id,
                OutpostRun.lease_until > now,
            )
            .values(lease_until=until)
            .execution_options(synchronize_session=False)
        )


async def _ended(
    session: AsyncSession,
    outpost: Outpost,
    instance: OutpostInstance,
    ended: list[OutpostRunEnded],
    now: datetime,
) -> None:
    """An offer this instance ran is `ended` with its exit code. One it was told to stop, or lost, keeps its
    state and takes the exit code it reports."""
    for each in ended:
        mine = (
            OutpostRun.outpost_id == outpost.id,
            OutpostRun.id == each.id,
            OutpostRun.instance_id == instance.id,
        )
        done = await session.execute(
            update(OutpostRun)
            .where(*mine, OutpostRun.state == offers.CLAIMED.value)
            .values(state=offers.ENDED.value, exit_code=each.exit_code, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        if done.rowcount == 1:  # type: ignore[attr-defined]
            continue
        await session.execute(
            update(OutpostRun)
            .where(
                *mine,
                OutpostRun.state.in_([offers.LOST.value, offers.CANCELLED.value]),
                OutpostRun.exit_code.is_(None),
            )
            .values(exit_code=each.exit_code)
            .execution_options(synchronize_session=False)
        )


# ───────────────────────── what a poll is handed ─────────────────────────


async def claim(
    session: AsyncSession,
    outpost: Outpost,
    instance: OutpostInstance,
    runner: OutpostRunner | None,
    now: datetime,
) -> list[dict[str, str]]:
    """Claim offers for the instance that polls and return them as they cross the door: each sealed to that
    instance's key and signed by the door. Nothing while the outpost is not active or the instance not
    approved, nothing for an instance that does not announce `runs`, nothing beyond its capacity, and nothing
    from a door that cannot sign: no daemon would start the run."""
    if runner is None:
        return []
    capacity = min(runner.run_capacity, MAX_RUN_CAPACITY)
    if capacity <= 0 or outpost.status != _ACTIVE or instance.status != OutpostInstanceStatus.ACTIVE.value:
        return []
    if outpost.owner_user_id is None or _RUNS not in (outpost.capabilities or []):
        return []
    private_pem = get_settings().outposts_door_signing_key
    if not private_pem:
        return []
    waiting = await session.execute(
        select(OutpostRun)
        .where(
            OutpostRun.outpost_id == outpost.id,
            OutpostRun.state == offers.OFFERED.value,
            OutpostRun.claimed_at.is_(None),
            OutpostRun.expires_at > now,
            OutpostRun.kind.in_([kind.value for kind in runner.offer_kinds]),
            OutpostRun.isolation.in_([ISOLATION_OF_MODE[mode] for mode in runner.modes]),
            or_(
                OutpostRun.credential != RunCredential.SUBSCRIPTION.value,
                OutpostRun.provider.in_([provider.value for provider in runner.subscriptions]),
            ),
        )
        .order_by(OutpostRun.created_at, OutpostRun.id)
        .limit(capacity)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    handed: list[dict[str, str]] = []
    for offer in list(waiting.scalars()):
        element = await _hand_over(session, outpost, instance, offer, now, private_pem)
        if element is not None:
            handed.append(element)
    return handed


class _TooLargeError(Exception):
    """An offer heavier than the door carries: its claim is undone, it is not handed over."""


async def _hand_over(
    session: AsyncSession,
    outpost: Outpost,
    instance: OutpostInstance,
    offer: OutpostRun,
    now: datetime,
    private_pem: str,
) -> dict[str, str] | None:
    """Claim the offer's row for the instance, then mint its token, seal it and sign it: `None` when the
    row was claimed since it was read, or when the offer cannot cross the door, which then leaves the row
    as it was (a savepoint): no token is minted for an offer this instance did not win."""
    lease_until = now + timedelta(seconds=min(offer.lease_s, offer.max_minutes * 60))
    try:
        async with session.begin_nested():
            won = await session.execute(
                update(OutpostRun)
                .where(
                    OutpostRun.outpost_id == outpost.id,
                    OutpostRun.id == offer.id,
                    OutpostRun.state == offers.OFFERED.value,
                    OutpostRun.claimed_at.is_(None),
                )
                .values(
                    state=offers.CLAIMED.value,
                    instance_id=instance.id,
                    epoch=outpost.epoch,
                    claimed_at=now,
                    lease_until=lease_until,
                )
                .execution_options(synchronize_session=False)
            )
            if won.rowcount != 1:  # type: ignore[attr-defined]
                return None
            kind = OfferKind(offer.kind)
            token = mint_run_token(
                offer.token_subject,
                project_slug=offer.project_slug,
                work_item_key=offer.work_item_key,
                ttl_minutes=offer.max_minutes + offers.TOKEN_MARGIN_MINUTES[kind],
            )
            sealed = sealing.seal(instance.sealing_key, offer.id, offers.plaintext(offer, token))
            element = {
                "id": offer.id,
                "kind": kind.value,
                "sealed": sealed,
                "signature": rendezvous.sign_run(
                    private_pem, run_id=offer.id, instance_id=instance.id, sealed=sealed
                ),
            }
            if rendezvous.carried_bytes(element) > MAX_SEALED_RUN_BYTES:
                raise _TooLargeError
    except _TooLargeError:
        logger.error("outposts.run_too_large", offer=offer.id, outpost=outpost.id)
        return None
    except Exception as error:
        # This process cannot mint or seal (a run-token key it cannot use, a sealing key that is not one):
        # the offer is not handed over, here, and waits; the others are, and it says so.
        logger.error("outposts.run_not_handed", offer=offer.id, outpost=outpost.id, error=str(error)[:200])
        return None
    logger.info("outposts.run_claimed", offer=offer.id, outpost=outpost.id, instance=instance.id)
    return element


# ───────────────────────── what a poll is told to stop ─────────────────────────


async def no_longer_awaited(
    session: AsyncSession, outpost: Outpost, instance: OutpostInstance, running: list[str], now: datetime
) -> list[str]:
    """The poll's `cancel_runs`: among the offers the instance lists, those the door waits for no more (an
    offer it does not hold under a running lease: lost, withdrawn, ended, another instance's, unknown), and,
    once each, the offers it held that were lost or withdrawn since, listed or not."""
    cancel: list[str] = []
    if running:
        rows = await session.execute(
            select(OutpostRun.id, OutpostRun.state, OutpostRun.instance_id, OutpostRun.lease_until).where(
                OutpostRun.outpost_id == outpost.id, OutpostRun.id.in_(running)
            )
        )
        awaited = {
            str(offer_id)
            for offer_id, state, holder, lease_until in rows.all()
            if state == offers.CLAIMED.value
            and holder == instance.id
            and lease_until is not None
            and _aware(lease_until) > now
        }
        cancel = [offer_id for offer_id in dict.fromkeys(running) if offer_id not in awaited]
    untold = list(
        (
            await session.execute(
                select(OutpostRun.id)
                .where(
                    OutpostRun.outpost_id == outpost.id,
                    OutpostRun.instance_id == instance.id,
                    OutpostRun.state.in_([offers.LOST.value, offers.CANCELLED.value]),
                    OutpostRun.cancel_told_at.is_(None),
                    OutpostRun.exit_code.is_(None),
                )
                .order_by(OutpostRun.finished_at, OutpostRun.id)
                .limit(MAX_RUNS_RUNNING)
            )
        ).scalars()
    )
    if untold:
        await session.execute(
            update(OutpostRun)
            .where(OutpostRun.outpost_id == outpost.id, OutpostRun.id.in_(untold))
            .values(cancel_told_at=now)
            .execution_options(synchronize_session=False)
        )
    return list(dict.fromkeys([*cancel, *untold]))[:MAX_RUNS_RUNNING]


__all__ = ["ISOLATION_OF_MODE", "check_the_runs_lane", "claim", "no_longer_awaited", "report", "serves"]
