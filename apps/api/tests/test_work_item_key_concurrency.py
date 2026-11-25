"""Concurrent creations on one project each get their own key (#2284).

`internal_work_item_key` reads `max(n) + 1` over the project's keys. Two creations in flight read the
same number, and the uniqueness constraint `(project_id, tracker_key)` turned the second into an HTTP
500: on a real PostgreSQL, 10 at a time on one project, 84 of 100 answered 500
(`docs/load-testing.md`, bottleneck 4). The number is now minted under a per-project
`pg_advisory_xact_lock` held until the transaction ends.

The concurrent tests run on PostgreSQL only (CI's `python-postgres` job, "API suite on PostgreSQL",
`TRANIBOR_TEST_SUITE_ON_POSTGRES=1`): SQLite writes one transaction at a time and has no advisory lock.
The statement itself, and its place before the read of the keys, is checked on every backend by
`test_on_postgresql_the_lock_is_asked_for_before_the_keys_are_read`.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from .conftest import SUITE_ON_POSTGRES

pytestmark = pytest.mark.asyncio

AT_ONCE = 20

on_postgres = pytest.mark.skipif(
    not SUITE_ON_POSTGRES,
    reason="concurrent transactions, an advisory lock: PostgreSQL only (TRANIBOR_TEST_SUITE_ON_POSTGRES)",
)


@on_postgres
async def test_twenty_creations_at_once_on_one_project_each_get_a_key(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    """Through the route, each request in its own session and transaction, all in flight together."""
    from sqlalchemy import select
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.temporal import get_temporal

    async def create(n: int) -> Any:
        return await client.post(
            f"/api/v1/projects/{project['id']}/work-items", json={"title": f"Concurrent request {n}"}
        )

    responses = await asyncio.gather(*(create(n) for n in range(AT_ONCE)))

    assert [r.status_code for r in responses] == [201] * AT_ONCE, [
        (r.status_code, r.text[:200]) for r in responses if r.status_code != 201
    ]
    keys = [r.json()["tracker_key"] for r in responses]
    assert sorted(keys) == sorted(f"BILLING-API-{n}" for n in range(1, AT_ONCE + 1))
    async with session_scope() as session:
        stored = (
            (await session.execute(select(WorkItem.id).where(WorkItem.project_id == project["id"])))
            .scalars()
            .all()
        )
    assert len(stored) == AT_ONCE
    # Each interpreter started names a work item that was committed: none runs on a rolled-back row.
    started = {payload["work_item_id"] for payload in get_temporal().started.values()}  # type: ignore[attr-defined]
    assert started == set(stored)


@on_postgres
async def test_twenty_findings_minted_at_once_in_their_own_sessions_each_get_a_key(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    """The orchestrator's path (`internal_ticket_key`, a promoted finding): its own session, the key
    minted, the work item born, the commit — twenty of them at once."""
    from sqlalchemy import select
    from tranibor_api.db.models import Project, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.routing import WorkItemBirth, new_work_item
    from tranibor_api.services.tickets import internal_work_item_key

    async def promote(n: int) -> str:
        async with session_scope(orgs="*") as session:
            row = await session.get(Project, project["id"])
            assert row is not None
            key = await internal_work_item_key(session, row)
            await new_work_item(session, row, WorkItemBirth(tracker_key=key, title=f"Finding {n}"))
        return key

    outcomes = await asyncio.gather(*(promote(n) for n in range(AT_ONCE)), return_exceptions=True)

    failures = [repr(o)[:200] for o in outcomes if isinstance(o, BaseException)]
    assert failures == []
    assert sorted(outcomes) == sorted(f"BILLING-API-{n}" for n in range(1, AT_ONCE + 1))  # type: ignore[type-var]
    async with session_scope() as session:
        count = len(
            (await session.execute(select(WorkItem.id).where(WorkItem.project_id == project["id"]))).all()
        )
    assert count == AT_ONCE


class _Recorder:
    """A session that records its statements: the connector lookup finds none, the keys read finds two."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement: Any, parameters: Any = None) -> Any:
        sql = str(statement)
        self.statements.append((sql, parameters))
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: ["BILLING-API-1", "BILLING-API-7"]),
        )


async def test_on_postgresql_the_lock_is_asked_for_before_the_keys_are_read() -> None:
    """The SQLite suite cannot take a PostgreSQL lock; it checks one is asked for, on the project, and
    before the keys are read — taken after, two creations would still read the same `max`."""
    from tranibor_api.services import tickets

    session = _Recorder()
    project = SimpleNamespace(id="project-1", slug="billing-api")

    key = await tickets.internal_work_item_key(session, project)  # type: ignore[arg-type]

    assert key == "BILLING-API-8"
    statements = [sql for sql, _ in session.statements]
    lock = statements.index("SELECT pg_advisory_xact_lock(:n)")
    expected = int.from_bytes(hashlib.sha256(b"work-item-key:project-1").digest()[:8], "big", signed=True)
    assert session.statements[lock][1] == {"n": expected}
    (read,) = [i for i, sql in enumerate(statements) if "work_items.tracker_key" in sql and "LIKE" in sql]
    assert lock < read, statements


async def test_a_prefix_alone_mints_nothing_and_locks_nothing() -> None:
    """A schedule or a trigger builds its own key on the prefix (S38-11): no number, so no lock."""
    from tranibor_api.services import tickets

    session = _Recorder()
    project = SimpleNamespace(id="project-1", slug="billing-api")

    assert await tickets.internal_work_item_key(session, project, numerotee=False) == "BILLING-API"  # type: ignore[arg-type]
    assert not any("pg_advisory" in sql for sql, _ in session.statements)
