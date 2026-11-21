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
        row = (await session.execute(select(Organization).where(Organization.slug == org))).scalar_one()
        row.name = "Again"
        assert not wrote_in_transaction(session), "a pending change takes no lock until it is flushed"
        await session.flush()
        assert wrote_in_transaction(session), "a flush is a write"
        await session.rollback()
        assert not wrote_in_transaction(session), "a rollback ends it"
