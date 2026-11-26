# SPDX-License-Identifier: Apache-2.0
"""Retention of the audit chains (ADR 0221): forget a prefix behind a checkpoint, keep the rest verifiable.

The rule is the deployment's (`TRANIBOR_AUDIT_RETENTION_DAYS`, 0 = keep for ever, the default) and may be
made longer for an organisation (`TRANIBOR_AUDIT_RETENTION_ORGS`), never shorter. A pass, per chain, in
transactions of at most `batch` entries:

1. lock the chain's head row (`FOR UPDATE SKIP LOCKED`, the export's style, ADR 0207): another replica that
   is pruning this chain makes this one skip it, and a second pass after the first finds nothing left;
2. start after the chain's latest `prune` checkpoint and read the next entries in the order of `seq`;
3. walk them with `ChainWalker` and keep the longest prefix that is **verified**, **older than the rule** and,
   when a SIEM export covers the chain, **already shipped** (the sink's cursor): a prefix that does not
   verify is never removed, since removing it would bury the break;
4. write the `prune` checkpoint (the last removed entry's `seq` and `hash`, the count, the rule, the time),
   delete the prefix, and record the prune in the chain as an `audit.retention.prune` entry, in one
   transaction. The database refuses the delete unless the checkpoint is already there (migration
   `e8c0a2d4f6b1`).

Replaying a pass has the effect of running it once. The loop runs in every API replica.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from prometheus_client import Counter as PromCounter
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core import utcnow

from .audit import record
from .audit_chain import (
    PLATFORM,
    PRUNE_ACTION,
    ChainWalker,
    Checkpoint,
    chains,
    links_after,
    stored_checkpoints,
)
from .config import Settings
from .db.models import AuditChainHead, AuditCheckpoint, AuditExportCursor, AuditLog
from .db.session import ALL_ORGS, session_scope
from .logging import get_logger
from .metrics import registry

log = get_logger("choregos.audit.retention")

#: The shortest rule accepted: a typo (`1` for `365`) must not empty the log.
MIN_DAYS = 30
#: The longest wait between two passes after consecutive failures.
MAX_BACKOFF_S = 3600.0

pruned_total = PromCounter(
    "tranibor_audit_retention_pruned_entries_total",
    "Audit entries removed by the retention rule (this process)",
    registry=registry,
)
held_back_total = PromCounter(
    "tranibor_audit_retention_held_back_total",
    "Prunes held below the cutoff because the SIEM sink had not been given the entries (this process)",
    registry=registry,
)
failures_total = PromCounter(
    "tranibor_audit_retention_failures_total",
    "Chains whose prune raised an error (this process)",
    registry=registry,
)


def _utc(moment: datetime) -> datetime:
    """SQLite returns naive times: they are UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass
class ChainResult:
    """What one chain's prune did."""

    label: str
    pruned: int = 0
    #: Another replica holds the chain.
    skipped: bool = False
    #: Entries old enough to go that the SIEM sink has not been given yet, so they stay.
    held_back: bool = False
    #: The walk found a break in the entries due to go: they stay, and `verify` says where.
    broken: str | None = None
    #: Dry run only: how many would go (at most one batch).
    would_prune: int = 0


@dataclass
class PassResult:
    chains: list[ChainResult] = field(default_factory=list)

    @property
    def pruned(self) -> int:
        return sum(item.pruned for item in self.chains)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.chains if item.broken)


def rules_from(settings: Settings) -> tuple[int, dict[str, int]]:
    """The deployment's days and each organisation's, checked. `ValueError` says what is wrong: the API
    refuses to start on it."""
    days, orgs = settings.audit_retention_days, settings.audit_retention_orgs
    if days < 0 or (0 < days < MIN_DAYS):
        raise ValueError(f"audit retention: TRANIBOR_AUDIT_RETENTION_DAYS is 0 (keep) or at least {MIN_DAYS}")
    if orgs and days == 0:
        raise ValueError(
            "audit retention: an organisation's rule needs the deployment's (it can only be longer)"
        )
    short = sorted(slug for slug, value in orgs.items() if value < days)
    if short:
        raise ValueError(
            f"audit retention: {', '.join(short)}: an organisation's rule is never shorter than the "
            f"deployment's {days} days"
        )
    return days, orgs


class Pruner:
    def __init__(
        self,
        days: int,
        *,
        org_days: dict[str, int] | None = None,
        export_sink: str | None = None,
        export_orgs: list[str] | None = None,
        export_platform: bool = True,
        batch: int = 5000,
        scope: Callable[..., AbstractAsyncContextManager[AsyncSession]] = session_scope,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.days = days
        self.org_days = org_days or {}
        self.export_sink = export_sink
        self.export_orgs = export_orgs or []
        self.export_platform = export_platform
        self.batch = max(1, batch)
        self._scope = scope
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> Pruner:
        days, orgs = rules_from(settings)
        return cls(
            days,
            org_days=orgs,
            export_sink=settings.audit_export_name if settings.audit_export else None,
            export_orgs=settings.audit_export_orgs,
            export_platform=settings.audit_export_platform,
            batch=settings.audit_retention_batch,
        )

    def days_for(self, label: str, chain: str) -> int:
        """A chain's rule: the deployment's, or the longer one set for its organisation (never for the
        platform's chain)."""
        return self.days if chain == PLATFORM else max(self.days, self.org_days.get(label, 0))

    async def _exported(self, session: AsyncSession) -> set[str] | None:
        """The chains the SIEM export covers, whose entries wait for the sink; `None` without an export."""
        if self.export_sink is None:
            return None
        try:
            return {chain for chain, _ in await chains(session, self.export_orgs, self.export_platform)}
        except LookupError:  # a slug of the export's filter that is no organisation (yet): hold everything
            log.warning("audit.retention.export_filter_unknown", orgs=self.export_orgs)
            return {chain for chain, _ in await chains(session)}

    async def run_pass(self, *, dry_run: bool = False, orgs: list[str] | None = None) -> PassResult:
        """Every chain, once (each until its prunable prefix is gone). A chain that fails does not stop the
        others."""
        result = PassResult()
        if self.days <= 0:
            return result
        async with self._scope(orgs=ALL_ORGS) as session:
            targets = await chains(session, orgs or [])
            exported = await self._exported(session)
        for chain, label in targets:
            gated = exported is not None and chain in exported
            try:
                result.chains.append(await self.prune_chain(chain, label, gated=gated, dry_run=dry_run))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures_total.inc()
                log.warning("audit.retention.failed", org=label, error=str(error)[:300])
                result.chains.append(ChainResult(label, broken=f"error: {str(error)[:200]}"))
        if result.pruned:
            log.info("audit.retention.pruned", entries=result.pruned)
        return result

    async def prune_chain(self, chain: str, label: str, *, gated: bool, dry_run: bool = False) -> ChainResult:
        total = ChainResult(label)
        days = self.days_for(label, chain)
        while True:
            async with self._scope(orgs=ALL_ORGS) as session:
                step = await self._batch(session, chain, label, days, gated, dry_run)
            total.pruned += step.pruned
            total.would_prune += step.would_prune
            total.skipped = total.skipped or step.skipped
            total.held_back = total.held_back or step.held_back
            total.broken = total.broken or step.broken
            if dry_run or step.pruned < self.batch or step.broken or step.skipped:
                return total

    async def _batch(
        self, session: AsyncSession, chain: str, label: str, days: int, gated: bool, dry_run: bool
    ) -> ChainResult:
        out = ChainResult(label)
        owner = AuditChainHead.chain == chain
        if (await session.execute(select(AuditChainHead.chain).where(owner))).first() is None:
            return out  # no entry yet
        head = (
            await session.execute(select(AuditChainHead).where(owner).with_for_update(skip_locked=True))
        ).scalar_one_or_none()
        if head is None:
            out.skipped = True
            return out
        floor = await self._floor(session, chain)
        ceiling = head.seq
        if gated:
            cursor = (
                await session.execute(
                    select(AuditExportCursor.seq).where(
                        AuditExportCursor.sink == self.export_sink, AuditExportCursor.chain == chain
                    )
                )
            ).scalar_one_or_none()
            ceiling = min(ceiling, int(cursor or 0))
        cutoff = self._clock() - timedelta(days=days)
        links = await links_after(
            session,
            chain,
            floor.seq if floor else 0,
            min(self.batch, max(0, ceiling - (floor.seq if floor else 0))),
        )
        walker = ChainWalker(chain, label, [floor] if floor else [])
        due = []
        for link in links:
            if _utc(link.ts) >= cutoff:
                break
            if not walker.feed(link):
                brk = walker.report.first_break
                out.broken = brk.reason if brk else "the chain does not verify"
                log.warning("audit.retention.chain_broken", org=label, reason=out.broken)
                break
            due.append(link)
        if gated and out.broken is None and len(due) == len(links) and ceiling < head.seq:
            after = await links_after(session, chain, ceiling, 1)
            if after and _utc(after[0].ts) < cutoff:
                out.held_back = True
                held_back_total.inc()
                log.warning(
                    "audit.retention.held_back", org=label, shipped_through=ceiling, chain_end=head.seq
                )
        if not due:
            return out
        last = due[-1]
        assert last.seq is not None and last.hash is not None  # `links_after` returns chained entries only
        if dry_run:
            out.would_prune = len(due)
            return out
        now = self._clock()
        session.add(
            AuditCheckpoint(
                org_id=chain or None,
                seq=last.seq,
                hash=last.hash,
                kind="prune",
                created_at=now,
                pruned=len(due),
                retention_days=days,
            )
        )
        await session.flush()
        owner_rows = AuditLog.org_id.is_(None) if chain == PLATFORM else AuditLog.org_id == chain
        deleted = await session.execute(
            delete(AuditLog).where(owner_rows, AuditLog.seq.is_not(None), AuditLog.seq <= last.seq)
        )
        removed = cast("CursorResult[Any]", deleted).rowcount
        if removed != len(due):  # a fork, or a writer that was not in line: nothing is removed
            raise RuntimeError(f"{label}: {removed} entries match the prefix of {len(due)}; not pruned")
        await record(
            session,
            None,
            PRUNE_ACTION,
            org_id=chain or None,
            target_type="audit_chain",
            target_id=chain or "platform",
            through_seq=last.seq,
            through_hash=last.hash,
            pruned=len(due),
            retention_days=days,
            held_by_export=gated,
        )
        out.pruned = len(due)
        pruned_total.inc(len(due))
        return out

    async def _floor(self, session: AsyncSession, chain: str) -> Checkpoint | None:
        """The chain's latest `prune` checkpoint: what is left starts after it."""
        found = [item for item in await stored_checkpoints(session, chain) if item.kind == "prune"]
        return max(found, key=lambda item: (item.seq, item.created_at)) if found else None


async def retention_loop(settings: Settings, pruner: Pruner | None = None) -> None:
    """Background task of the API (every replica). A failed pass does not stop the API; consecutive failures
    stretch the wait (twice each time, to `MAX_BACKOFF_S`)."""
    pruner = pruner or Pruner.from_settings(settings)
    interval = max(1.0, settings.audit_retention_interval_s)
    failures = 0
    log.info(
        "audit.retention.started",
        days=pruner.days,
        organisations=sorted(pruner.org_days),
        export=pruner.export_sink,
    )
    while True:
        try:
            result = await pruner.run_pass()
            failures = failures + 1 if result.failed else 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures += 1
            log.warning("audit.retention.pass_failed", error=str(error)[:200])
        await asyncio.sleep(min(MAX_BACKOFF_S, interval * 2 ** min(failures, 8)) if failures else interval)
