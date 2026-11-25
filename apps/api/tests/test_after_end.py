# SPDX-License-Identifier: Apache-2.0
"""`after_end` (#2031): what waits for a transaction's end runs once it commits AND once it rolls
back, in every owner of a session; `wrote_in_transaction` says whether the transaction already wrote."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import select, text, update


def _marker() -> tuple[list[str], Callable[[str], Callable[[], Any]]]:
    ran: list[str] = []

    def mark(name: str) -> Callable[[], Any]:
        async def callback() -> None:
            ran.append(name)

        return callback

    return ran, mark


async def test_session_scope_runs_it_after_a_commit_and_after_a_rollback(app: Any) -> None:
    from tranibor_api.db.session import after_commit, after_end, session_scope

    ran, mark = _marker()
    async with session_scope(orgs="*") as session:
        await session.execute(text("SELECT 1"))
        after_end(session, mark("committed"))
    with contextlib.suppress(RuntimeError):
        async with session_scope(orgs="*") as session:
            await session.execute(text("SELECT 1"))
            after_end(session, mark("rolled back"))
            after_commit(session, mark("never: its writes were not kept"))
            raise RuntimeError("the caller fails")
    assert ran == ["committed", "rolled back"]


async def _drive(owner: AsyncIterator[Any], body: Callable[[Any], Any], *, fail: bool) -> None:
    """Drive a FastAPI-style session dependency as FastAPI does: next(), then close or throw."""
    session = await owner.__anext__()
    await session.execute(text("SELECT 1"))  # what registers it has begun the transaction
    body(session)
    if fail:
        with contextlib.suppress(RuntimeError):
            await owner.athrow(RuntimeError("the route fails"))
    else:
        with contextlib.suppress(StopAsyncIteration):
            await owner.__anext__()


@pytest.mark.parametrize("dependency", ["get_db", "get_platform_db"])
@pytest.mark.parametrize("fail", [False, True], ids=["route-succeeds", "route-fails"])
async def test_a_request_session_runs_it_whatever_the_route_does(
    app: Any, dependency: str, fail: bool
) -> None:
    from tranibor_api import deps
    from tranibor_api.db.session import after_end

    ran, mark = _marker()
    await _drive(getattr(deps, dependency)(), lambda s: after_end(s, mark("ended")), fail=fail)
    assert ran == ["ended"]


async def test_wrote_in_transaction_follows_the_writes_of_the_current_transaction(app: Any, org: str) -> None:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope, wrote_in_transaction

    async with session_scope(orgs="*") as session:
        await session.execute(select(Organization.id))
        assert not wrote_in_transaction(session), "a read is not a write"
        await session.execute(update(Organization).where(Organization.slug == org).values(name="Renamed"))
        assert wrote_in_transaction(session), "an executed UPDATE is"
        await session.commit()
        assert not wrote_in_transaction(session), "a commit ends it"
    # A scope of its own: on PostgreSQL the organisations' scope lives for one transaction.
    async with session_scope(orgs="*") as session:
        row = (await session.execute(select(Organization).where(Organization.slug == org))).scalar_one()
        row.name = "Again"
        assert not wrote_in_transaction(session), "a pending change takes no lock until it is flushed"
        await session.flush()
        assert wrote_in_transaction(session), "a flush is a write"
        await session.rollback()
        assert not wrote_in_transaction(session), "a rollback ends it"


async def test_a_released_savepoint_is_not_a_commit(app: Any, org: str) -> None:
    """What waits for a commit, registered in a SAVEPOINT that was released, dies when the outer
    transaction rolls back (ADR 0085), and runs once when it commits."""
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import after_commit, session_scope

    ran, mark = _marker()
    for fail in (True, False):
        with contextlib.suppress(RuntimeError):
            async with session_scope(orgs="*") as session:
                await session.execute(update(Organization).where(Organization.slug == org).values(name="x"))
                async with session.begin_nested():
                    after_commit(session, mark(f"fail={fail}"))
                if fail:
                    raise RuntimeError("the outer transaction fails")
    assert ran == ["fail=False"]


async def test_a_released_savepoint_s_callback_dies_with_the_root_s_rollback(app: Any, org: str) -> None:
    """#2089: released, then the root rolls back and the session goes on to commit something else:
    the writes the callback belonged to were never kept, so it never runs (ADR 0085)."""
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import after_commit, session_scope

    ran, mark = _marker()
    async with session_scope(orgs="*") as session:
        await session.execute(update(Organization).where(Organization.slug == org).values(name="x"))
        async with session.begin_nested():
            after_commit(session, mark("never: rolled back"))
        await session.rollback()
        await session.execute(text("SELECT 1"))
        after_commit(session, mark("committed"))
    assert ran == ["committed"]


async def test_a_failed_savepoint_keeps_the_outer_transaction_s_callbacks(app: Any, org: str) -> None:
    """#2089: what the outer transaction registered survives a SAVEPOINT that rolls back, and runs
    once when the outer transaction commits; what the savepoint registered does not."""
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import after_commit, session_scope

    ran, mark = _marker()
    async with session_scope(orgs="*") as session:
        await session.execute(update(Organization).where(Organization.slug == org).values(name="x"))
        after_commit(session, mark("outer"))
        with contextlib.suppress(RuntimeError):
            async with session.begin_nested():
                after_commit(session, mark("never: its savepoint rolled back"))
                raise RuntimeError("the savepoint fails")
        async with session.begin_nested():
            after_commit(session, mark("released"))
    assert ran == ["outer", "released"]


async def test_nested_savepoints_drop_only_what_the_failed_one_registered(app: Any, org: str) -> None:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import after_commit, session_scope

    ran, mark = _marker()
    async with session_scope(orgs="*") as session:
        await session.execute(update(Organization).where(Organization.slug == org).values(name="x"))
        async with session.begin_nested():
            after_commit(session, mark("outer savepoint"))
            with contextlib.suppress(RuntimeError):
                async with session.begin_nested():
                    after_commit(session, mark("never: inner savepoint rolled back"))
                    raise RuntimeError("the inner savepoint fails")
            after_commit(session, mark("after the inner one"))
    assert ran == ["outer savepoint", "after the inner one"]


async def test_what_waits_for_the_end_waits_for_the_outer_transaction(app: Any, org: str) -> None:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import after_end, session_scope, wrote_in_transaction

    ran, mark = _marker()
    async with session_scope(orgs="*") as session:
        await session.execute(update(Organization).where(Organization.slug == org).values(name="x"))
        async with session.begin_nested():
            after_end(session, mark("ended"))
        assert wrote_in_transaction(session), "a released savepoint leaves the outer write lock"
        with contextlib.suppress(RuntimeError):
            async with session.begin_nested():
                raise RuntimeError("the savepoint fails")
        assert wrote_in_transaction(session), "a rolled-back savepoint leaves it too"
        assert ran == [], "not before the outer transaction ends"
    assert ran == ["ended"]


async def test_a_write_only_in_a_rolled_back_savepoint_still_counts(app: Any, org: str) -> None:
    """SQLite keeps the write lock until the outer transaction ends, whatever the savepoint did."""
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope, wrote_in_transaction

    async with session_scope(orgs="*") as session:
        with contextlib.suppress(RuntimeError):
            async with session.begin_nested():
                await session.execute(update(Organization).where(Organization.slug == org).values(name="y"))
                raise RuntimeError("the savepoint fails")
        assert wrote_in_transaction(session)


async def test_a_cancelled_scope_still_runs_what_waits_for_its_end(app: Any) -> None:
    import asyncio

    from tranibor_api.db.session import after_commit, after_end, session_scope

    ran, mark = _marker()
    with contextlib.suppress(asyncio.CancelledError):
        async with session_scope(orgs="*") as session:
            await session.execute(text("SELECT 1"))
            after_end(session, mark("ended"))
            after_commit(session, mark("never: nothing was committed"))
            raise asyncio.CancelledError
    assert ran == ["ended"]


async def test_a_cancellation_while_a_commit_s_callback_awaits_keeps_the_end_s(app: Any) -> None:
    """Committed, then cancelled while an `after_commit` callback (a workflow start) awaits: what
    waited for the end has already run."""
    import asyncio

    from tranibor_api.db.session import after_commit, after_end, session_scope

    ran, mark = _marker()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)

    async def scope() -> None:
        async with session_scope(orgs="*") as session:
            await session.execute(text("SELECT 1"))
            after_commit(session, slow)
            after_end(session, mark("ended"))

    task = asyncio.create_task(scope())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert ran == ["ended"]
