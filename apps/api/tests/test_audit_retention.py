"""Retention of the audit chains (ADR 0221), on SQLite with a chain written as the trigger links it.

What this file proves is the pruner's own behaviour: what it removes and what it refuses to, and that the
verifier still passes what remains from the checkpoint and still fails what was touched. The database's
refusal to delete without a checkpoint, and two replicas pruning at once, are PostgreSQL's:
`test_audit_retention_postgres.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from tranibor_api.audit_chain import (
    PRUNE_ACTION,
    ChainWalker,
    Checkpoint,
    links_after,
    sign,
    stored_checkpoints,
)
from tranibor_api.audit_export.exporter import Exporter
from tranibor_api.audit_export.sinks import FakeSink
from tranibor_api.audit_retention import MIN_DAYS, Pruner, retention_loop, rules_from
from tranibor_api.config import Settings
from tranibor_api.db.models import AuditChainHead, AuditCheckpoint, AuditExportCursor, AuditLog, Organization
from tranibor_api.db.session import session_scope
from tranibor_api.main import _audit_pruner

from .conftest import SUITE_ON_POSTGRES
from .kits.audit_chain import new_key
from .kits.audit_export import T0, write_chain
from .kits.audit_retention import CLOCK, trigger_on_sqlite

pytestmark = pytest.mark.skipif(
    SUITE_ON_POSTGRES, reason="the chain is the trigger's on PostgreSQL: test_audit_retention_postgres.py"
)


@pytest.fixture(autouse=True)
def _trigger() -> Iterator[None]:
    with trigger_on_sqlite():
        yield


@pytest.fixture
async def orgs(app: Any) -> dict[str, str]:
    async with session_scope(orgs="*") as session:
        rows = [Organization(slug=slug, name=slug.upper()) for slug in ("a", "b")]
        session.add_all(rows)
        await session.flush()
        return {row.slug: row.id for row in rows}


def pruner(**kwargs: Any) -> Pruner:
    return Pruner(30, clock=lambda: CLOCK, **kwargs)


async def seqs(org_id: str | None) -> list[int]:
    owner = AuditLog.org_id.is_(None) if org_id is None else AuditLog.org_id == org_id
    async with session_scope(orgs="*") as session:
        rows = (await session.execute(select(AuditLog.seq).where(owner).order_by(AuditLog.seq))).scalars()
        return [int(seq) for seq in rows if seq is not None]


async def walk(org_id: str, label: str = "a", anchors: list[Checkpoint] | None = None) -> Any:
    """`audit_chain.verify` for SQLite: the same walker over the same reads (`verify` itself reads the
    payload with PostgreSQL's `::text`)."""
    async with session_scope(orgs="*") as session:
        walker = ChainWalker(org_id, label, [*await stored_checkpoints(session, org_id), *(anchors or [])])
        for link in await links_after(session, org_id, 0, 10_000):
            if not walker.feed(link):
                break
        head = await session.get(AuditChainHead, org_id)
        return walker.finish((head.seq, head.hash) if head is not None else None)


async def checkpoints(org_id: str) -> list[AuditCheckpoint]:
    async with session_scope(orgs="*") as session:
        rows = await session.execute(
            select(AuditCheckpoint).where(AuditCheckpoint.org_id == org_id).order_by(AuditCheckpoint.seq)
        )
        return list(rows.scalars())


async def test_nothing_is_pruned_when_retention_is_off(orgs: dict[str, str]) -> None:
    """Off is the default, and a pruner whose rule is 0 touches nothing, however old the log is."""
    assert Settings().audit_retention_days == 0
    assert _audit_pruner(Settings()) is None
    await write_chain(orgs["a"], 10)

    result = await Pruner(0, clock=lambda: CLOCK + timedelta(days=3650)).run_pass()

    assert result.chains == [] and await seqs(orgs["a"]) == list(range(1, 11))
    assert await checkpoints(orgs["a"]) == []


async def test_pruning_keeps_the_rest_verifiable_from_the_checkpoint(orgs: dict[str, str]) -> None:
    end = await write_chain(orgs["a"], 10)
    before = await walk(orgs["a"])
    assert before.intact and before.entries == 10

    result = await pruner().run_pass()

    assert result.pruned == 5
    # Entries 1 to 5 are gone; the prune is recorded in the chain, as entry 11.
    assert await seqs(orgs["a"]) == [6, 7, 8, 9, 10, 11]
    (checkpoint,) = await checkpoints(orgs["a"])
    assert (checkpoint.kind, checkpoint.seq, checkpoint.pruned, checkpoint.retention_days) == (
        "prune",
        5,
        5,
        30,
    )
    after = await walk(orgs["a"])
    assert after.intact and after.entries == 6 and after.last_seq == 11
    assert after.pruned is not None and after.pruned.seq == 5
    assert after.as_dict()["pruned_through_seq"] == 5
    async with session_scope(orgs="*") as session:
        entry = (await session.execute(select(AuditLog).where(AuditLog.action == PRUNE_ACTION))).scalar_one()
        assert entry.payload["through_seq"] == 5 and entry.payload["pruned"] == 5
        assert entry.prev_hash == end  # chained after the old end: nothing was rewritten


async def test_the_same_deletion_without_a_checkpoint_is_a_break(orgs: dict[str, str]) -> None:
    """What the checkpoint is for: a prefix removed by hand is entries deleted."""
    await write_chain(orgs["a"], 10)
    async with session_scope(orgs="*") as session:
        await session.execute(delete(AuditLog).where(AuditLog.org_id == orgs["a"], AuditLog.seq <= 5))
    found = (await walk(orgs["a"])).first_break
    assert found is not None and found.seq == 1 and "missing" in found.reason


async def test_tampering_with_a_remaining_event_is_still_detected(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await pruner().run_pass()
    async with session_scope(orgs="*") as session:
        await session.execute(
            update(AuditLog).where(AuditLog.org_id == orgs["a"], AuditLog.seq == 8).values(payload={"n": 99})
        )
    found = (await walk(orgs["a"])).first_break
    assert found is not None and found.seq == 8 and "altered" in found.reason


async def test_the_first_remaining_event_must_link_to_the_checkpoint(orgs: dict[str, str]) -> None:
    """The checkpoint's hash is where the walk starts: change it and the entry after it no longer links."""
    await write_chain(orgs["a"], 10)
    await pruner().run_pass()
    async with session_scope(orgs="*") as session:
        await session.execute(
            update(AuditCheckpoint).where(AuditCheckpoint.kind == "prune").values(hash="f" * 64)
        )
    found = (await walk(orgs["a"])).first_break
    assert found is not None and found.seq == 6 and "does not link" in found.reason


async def test_a_prune_checkpoint_the_chain_does_not_record_is_refused(orgs: dict[str, str]) -> None:
    """Someone prunes further by hand: a correct checkpoint at entry 7 and entries 6 and 7 deleted. The
    chain links, but no `audit.retention.prune` entry says the checkpoint was written by a prune."""
    await write_chain(orgs["a"], 10)
    await pruner().run_pass()
    async with session_scope(orgs="*") as session:
        seven = (
            await session.execute(select(AuditLog).where(AuditLog.org_id == orgs["a"], AuditLog.seq == 7))
        ).scalar_one()
        session.add(
            AuditCheckpoint(
                org_id=orgs["a"], seq=7, hash=str(seven.hash), kind="prune", created_at=CLOCK, pruned=2
            )
        )
        await session.execute(delete(AuditLog).where(AuditLog.org_id == orgs["a"], AuditLog.seq <= 7))
    found = (await walk(orgs["a"])).first_break
    assert found is not None and "not recorded in the chain" in found.reason


async def test_a_checkpoint_kept_outside_must_agree_with_the_prune_point(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await pruner().run_pass()
    (checkpoint,) = await checkpoints(orgs["a"])
    key = new_key()
    naive = T0.replace(tzinfo=None)  # as SQLite hands the stored checkpoints back
    honest = sign(orgs["a"], 5, checkpoint.hash, naive, key)
    liar = sign(orgs["a"], 5, "e" * 64, naive, key)
    assert (await walk(orgs["a"], anchors=[honest])).intact
    found = (await walk(orgs["a"], anchors=[liar])).first_break
    assert found is not None and "vouched for" in found.reason


async def test_unshipped_entries_are_not_pruned(orgs: dict[str, str]) -> None:
    """With the SIEM export on, the cursor is the ceiling: entries the sink has not been given stay, whatever
    their age; with no cursor at all, nothing goes."""
    await write_chain(orgs["a"], 10)

    first = await pruner(export_sink="siem").run_pass()
    assert first.pruned == 0 and await seqs(orgs["a"]) == list(range(1, 11))

    async with session_scope(orgs="*") as session:
        session.add(
            AuditExportCursor(
                sink="siem", chain=orgs["a"], org_id=orgs["a"], seq=3, hash="0" * 64, updated_at=T0
            )
        )
    second = await pruner(export_sink="siem").run_pass()
    assert second.pruned == 3 and any(item.held_back for item in second.chains)
    assert (await seqs(orgs["a"]))[:1] == [4]

    async with session_scope(orgs="*") as session:
        await session.execute(update(AuditExportCursor).values(seq=10))
    third = await pruner(export_sink="siem").run_pass()
    assert third.pruned == 2  # up to the rule's cutoff now: entries 4 and 5
    assert (await walk(orgs["a"])).intact


async def test_a_real_export_then_a_prune(orgs: dict[str, str]) -> None:
    """The cursor the exporter moves is the one the pruner reads."""
    await write_chain(orgs["a"], 10)
    sink = FakeSink()
    await Exporter(sink, orgs=["a"], platform=False).run_pass()
    result = await pruner(export_sink="siem", export_orgs=["a"], export_platform=False).run_pass(orgs=["a"])
    assert result.pruned == 5 and len(sink.delivered) == 10
    # The export then goes on from its cursor without a discontinuity.
    await Exporter(FakeSink(), orgs=["a"], platform=False).run_pass()


async def test_a_chain_the_export_does_not_cover_is_not_held(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await write_chain(orgs["b"], 10)
    result = await pruner(export_sink="siem", export_orgs=["b"], export_platform=False).run_pass()
    assert await seqs(orgs["a"]) == [6, 7, 8, 9, 10, 11]  # `a` is not exported: only the rule counts
    assert await seqs(orgs["b"]) == list(range(1, 11)) and result.pruned == 5  # `b` waits for its cursor


async def test_a_prefix_that_does_not_verify_is_not_removed(orgs: dict[str, str]) -> None:
    """Removing a tampered entry would bury the break: the verified entries before it go, it and what
    follows stay, and the verifier still finds it."""
    await write_chain(orgs["a"], 10)
    async with session_scope(orgs="*") as session:
        await session.execute(
            update(AuditLog).where(AuditLog.org_id == orgs["a"], AuditLog.seq == 3).values(payload={"n": 99})
        )
    result = await pruner().run_pass()
    item = next(c for c in result.chains if c.label == "a")
    assert result.pruned == 2 and result.failed == 1
    assert "altered" in (item.broken or "")
    assert (await seqs(orgs["a"]))[:1] == [3]
    found = (await walk(orgs["a"])).first_break
    assert found is not None and found.seq == 3 and "altered" in found.reason

    again = await pruner().run_pass()
    assert again.pruned == 0 and (await seqs(orgs["a"]))[:1] == [3]


async def test_a_pass_twice_has_the_effect_of_once(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await pruner().run_pass()
    again = await pruner().run_pass()
    assert again.pruned == 0
    assert [c.seq for c in await checkpoints(orgs["a"])] == [5]
    async with session_scope(orgs="*") as session:
        count = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == PRUNE_ACTION)
        )
    assert count == 1


async def test_a_big_prefix_goes_in_batches_each_behind_its_checkpoint(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await pruner(batch=2).run_pass()
    assert [c.seq for c in await checkpoints(orgs["a"])] == [2, 4, 5]
    report = await walk(orgs["a"])
    assert report.intact and report.pruned is not None and report.pruned.seq == 5
    assert report.entries == 8  # entries 6 to 10, then the three records of the three batches (11 to 13)


async def test_an_organisation_may_keep_longer_never_shorter(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    await write_chain(orgs["b"], 10)
    clock = CLOCK + timedelta(days=15)  # every entry is 45 days old
    result = await Pruner(30, org_days={"a": 60}, clock=lambda: clock).run_pass()
    assert await seqs(orgs["a"]) == list(range(1, 11))  # a's own rule: 60 days
    assert await seqs(orgs["b"]) == [
        11
    ]  # b has the deployment's 30 days: all of it goes, and its record stays
    assert result.pruned == 10
    pruned = Pruner(30, org_days={"a": 10}, clock=lambda: clock)
    assert pruned.days_for("a", orgs["a"]) == 30  # a shorter one counts for nothing


def test_the_settings_refuse_a_rule_that_could_not_be_kept() -> None:
    assert rules_from(Settings()) == (0, {})
    assert rules_from(Settings(audit_retention_days=365, audit_retention_orgs={"a": 730})) == (
        365,
        {"a": 730},
    )
    for bad in (
        {"audit_retention_days": 1},
        {"audit_retention_days": -5},
        {"audit_retention_days": 90, "audit_retention_orgs": {"a": 30}},
        {"audit_retention_days": 0, "audit_retention_orgs": {"a": 400}},
    ):
        with pytest.raises(ValueError, match="audit retention"):
            rules_from(Settings(**bad))
    assert MIN_DAYS == 30


async def test_the_loop_prunes_and_stops_when_cancelled(orgs: dict[str, str]) -> None:
    await write_chain(orgs["a"], 10)
    settings = Settings(audit_retention_days=30, audit_retention_interval_s=1.0)
    task = asyncio.create_task(retention_loop(settings, pruner()))
    for _ in range(50):
        if await seqs(orgs["a"]) and (await seqs(orgs["a"]))[0] == 6:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await seqs(orgs["a"]))[0] == 6
