"""Retention of the audit chains on the real database (ADR 0221).

PostgreSQL only (`TRANIBOR_TEST_DATABASE_URL`): the schema of `python -m tranibor_api.migrate` (migration
`e8c0a2d4f6b1`), the API's non-superuser role that owns the tables, the trigger that links every entry and
the one that refuses a delete. What SQLite cannot show: that the database refuses a delete no checkpoint
vouches for, that the walk of what remains passes from the checkpoint with the verifier's own SQL, and that
two replicas pruning one chain never delete twice.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from tranibor_api.audit_retention import Pruner
from tranibor_core import utcnow

from .conftest import PG_URL, _administer, without_postgres

pytestmark = without_postgres


def old() -> Any:
    """A clock a hundred days on: every entry written by the test is older than a 30-day rule."""
    moment = utcnow() + timedelta(days=100)
    return lambda: moment


@pytest.fixture
async def orgs(pg_app: Any) -> dict[str, str]:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        rows = [Organization(slug=slug, name=slug.upper()) for slug in ("a", "b")]
        session.add_all(rows)
        await session.flush()
        return {row.slug: row.id for row in rows}


async def _write(scope: Any, org_id: str | None, action: str, **payload: Any) -> None:
    from tranibor_api.audit import record
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs=scope) as session:
        await record(session, None, action, org_id=org_id, target_type="test", target_id=action, **payload)


async def _fill(org_id: str, count: int = 8) -> None:
    for n in range(count):
        await _write("*", org_id, "a.thing", n=n)


async def _verify(chain: str, **kwargs: Any) -> Any:
    from tranibor_api.audit_chain import verify
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return await verify(session, chain, "x", **kwargs)


async def _seqs(org_id: str) -> list[int]:
    from tranibor_api.db.models import AuditLog
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        rows = await session.execute(
            select(AuditLog.seq).where(AuditLog.org_id == org_id).order_by(AuditLog.seq)
        )
        return [int(seq) for seq in rows.scalars()]


async def test_what_remains_verifies_from_the_checkpoint(orgs: dict[str, str]) -> None:
    await _fill(orgs["a"])
    assert (await _verify(orgs["a"])).entries == 8

    result = await Pruner(30, clock=old()).run_pass()

    assert result.pruned == 8
    assert await _seqs(orgs["a"]) == [9]  # the record of the prune, chained after entry 8
    report = await _verify(orgs["a"])
    assert report.intact and report.pruned is not None and report.pruned.seq == 8 and report.entries == 2
    again = await Pruner(30, clock=old()).run_pass()
    assert again.pruned == 0


async def test_the_database_refuses_a_delete_no_checkpoint_vouches_for(orgs: dict[str, str]) -> None:
    from tranibor_api.db.session import session_scope

    await _fill(orgs["a"], 4)
    with pytest.raises(DBAPIError, match="append-only"):
        async with session_scope(orgs="*") as session:
            await session.execute(text(f"DELETE FROM audit_log WHERE org_id = '{orgs['a']}' AND seq = 1"))

    # A checkpoint at entry 2: entries 1 and 2 may go, entry 3 may not, and nothing may be updated.
    async with session_scope(orgs="*") as session:
        await session.execute(
            text(
                "INSERT INTO audit_checkpoints (id, org_id, seq, hash, kind, created_at, pruned) "
                f"SELECT gen_random_uuid()::text, org_id, seq, hash, 'prune', now(), 2 FROM audit_log "
                f"WHERE org_id = '{orgs['a']}' AND seq = 2"
            )
        )
        await session.execute(text(f"DELETE FROM audit_log WHERE org_id = '{orgs['a']}' AND seq <= 2"))
    assert await _seqs(orgs["a"]) == [3, 4]
    for statement in (
        f"DELETE FROM audit_log WHERE org_id = '{orgs['a']}' AND seq = 3",
        f"UPDATE audit_log SET action = 'x' WHERE org_id = '{orgs['a']}' AND seq = 3",
        "DELETE FROM audit_checkpoints",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with session_scope(orgs="*") as session:
                await session.execute(text(statement))


async def test_a_session_bounded_to_an_organisation_cannot_prune_or_vouch(orgs: dict[str, str]) -> None:
    """The organisation's own scope sees its checkpoints but writes none (the platform's scope does), and
    the entries it can see past the last checkpoint are refused a delete as before."""
    from tranibor_api.db.session import session_scope

    await _fill(orgs["a"], 3)
    await Pruner(30, clock=old()).run_pass()
    await _fill(orgs["a"], 2)
    with pytest.raises(DBAPIError):
        async with session_scope(orgs=["a"]) as session:
            await session.execute(text(f"DELETE FROM audit_log WHERE org_id = '{orgs['a']}'"))
    with pytest.raises(DBAPIError):
        async with session_scope(orgs=["a"]) as session:
            await session.execute(
                text(
                    "INSERT INTO audit_checkpoints (id, org_id, seq, hash, kind, created_at) "
                    f"VALUES (gen_random_uuid()::text, '{orgs['a']}', 5, repeat('0', 64), 'prune', now())"
                )
            )


async def test_tampering_with_a_remaining_event_is_still_detected(orgs: dict[str, str]) -> None:
    await _fill(orgs["a"], 6)
    await Pruner(30, clock=lambda: utcnow() + timedelta(days=100), batch=3).run_pass()
    await _fill(orgs["a"], 3)  # entries after the prune
    await _administer(
        PG_URL,
        "SET session_replication_role = replica",
        f"UPDATE audit_log SET payload = '{{\"n\": 99}}' WHERE org_id = '{orgs['a']}' AND action = 'a.thing' "
        f"AND payload @> '{{\"n\": 1}}' AND seq > 6",
    )
    report = await _verify(orgs["a"])
    assert report.first_break is not None and "altered" in report.first_break.reason


async def test_a_prune_checkpoint_added_by_hand_is_refused(orgs: dict[str, str]) -> None:
    await _fill(orgs["a"], 6)
    await _administer(
        PG_URL,
        "SET session_replication_role = replica",
        "INSERT INTO audit_checkpoints (id, org_id, seq, hash, kind, created_at, pruned) "
        f"SELECT gen_random_uuid()::text, org_id, seq, hash, 'prune', now(), 3 FROM audit_log "
        f"WHERE org_id = '{orgs['a']}' AND seq = 3",
        f"DELETE FROM audit_log WHERE org_id = '{orgs['a']}' AND seq <= 3",
    )
    report = await _verify(orgs["a"])
    assert report.first_break is not None and "not recorded in the chain" in report.first_break.reason


async def test_unshipped_entries_are_not_pruned(orgs: dict[str, str]) -> None:
    from tranibor_api.audit_export.exporter import Exporter
    from tranibor_api.audit_export.sinks import FakeSink

    await _fill(orgs["a"], 5)
    gated = Pruner(30, export_sink="siem", export_orgs=["a"], export_platform=False, clock=old())
    assert (await gated.run_pass()).pruned == 0 and await _seqs(orgs["a"]) == [1, 2, 3, 4, 5]

    await Exporter(FakeSink(), orgs=["a"], platform=False).run_pass()
    assert (await gated.run_pass()).pruned == 5
    assert (await _verify(orgs["a"])).intact


async def test_two_replicas_pruning_one_chain_never_delete_twice(orgs: dict[str, str]) -> None:
    """The chain's head row is locked `FOR UPDATE SKIP LOCKED`: the second replica skips the chain the first
    holds (the sleep keeps the first inside its transaction), and what the first removed is not removed
    again: one checkpoint, one record, every entry counted once."""
    await _fill(orgs["a"], 6)

    class Slow(Pruner):
        async def _floor(self, session: Any, chain: str) -> Any:
            await asyncio.sleep(0.4)  # the other replica's pass runs while this one holds the chain
            return await super()._floor(session, chain)

    first, second = Slow(30, clock=old()), Slow(30, clock=old())
    results = await asyncio.gather(first.run_pass(orgs=["a"]), second.run_pass(orgs=["a"]))

    chains = [item for result in results for item in result.chains if item.label == "a"]
    assert sum(item.pruned for item in chains) == 6 and sum(item.skipped for item in chains) == 1
    from tranibor_api.db.models import AuditCheckpoint
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditCheckpoint)
            .where(AuditCheckpoint.kind == "prune", AuditCheckpoint.org_id == orgs["a"])
        )
    assert count == 1 and await _seqs(orgs["a"]) == [7]
    assert (await _verify(orgs["a"])).intact
