# SPDX-License-Identifier: Apache-2.0
"""The interpreter's test helpers (the test kit, ADR 0181 round D): a stage scripted on the fake executor,
a work item's `WorkflowInterpreter` started, a state or a condition waited for through its `status` query,
the runs counted. They were `apps/orchestrator/tests/test_interpreter.py`'s, which keeps its former names.
"""

from __future__ import annotations

from typing import Any

from tranibor_contracts import StageResult

from .orchestrator import Fixture


def scripted(adapters: Any, *results: StageResult) -> None:
    for result in results:
        adapters.executor.queue_result(result)


async def start(env: Any, setup: Fixture, task_queue: str = "test") -> Any:
    return await env.client.start_workflow(
        "WorkflowInterpreter",
        {
            "project_id": setup.project_id,
            "project_slug": setup.project_slug,
            "work_item_id": setup.work_item_id,
            "tracker_key": setup.tracker_key,
        },
        id=f"wi-{setup.project_slug}-123",
        task_queue=task_queue,
    )


async def runs(setup: Fixture) -> int:
    """How many agent steps ran for this ticket."""
    from sqlalchemy import func, select
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(Run).where(Run.work_item_id == setup.work_item_id)
                )
            ).scalar_one()
        )


def pr_ref(setup: Fixture) -> Any:
    from tranibor_core.domain import PrRef

    prs = list(setup.adapters.scm.prs.values())
    if prs:
        return prs[0].ref
    return PrRef(repo="northwind/billing-api", number=1)


async def wait_until(handle: Any, description: str, predicate: Any, *, timeout: int = 60) -> None:
    """Waits for a CONDITION to be true on the workflow `status`.

    Waiting for a state by equality only works for a **stable** state: one where the workflow
    stops (a human wait, an end). For a passing state, 100 ms polling misses it
    one time in two: `test_pause_and_resume` waited for `in_progress` after a resume and went
    red in CI on `pr_open`, because the following steps had already passed. A test that
    fails depending on the machine load says nothing about the code.
    """
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    status: Any = None
    while time.monotonic() < deadline:
        status = await query_status(handle)
        if status is not None and predicate(status):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{description}: never true (last status: {status})")


async def query_status(handle: Any) -> Any:
    """One `status` query, or None when it outlasted its deadline (#492, #496).

    On a loaded machine the time-skipping test server can reject a workflow-task completion
    ("invalid history builder state for action"); until the task is retried, a query hangs. The
    server then answers DEADLINE_EXCEEDED, and our own `rpc_timeout` ends it as CANCELLED
    ("Timeout expired"). That says nothing about the workflow, so the query gets a short deadline
    and the caller asks again until its own timeout.
    """
    from datetime import timedelta

    from temporalio.service import RPCError, RPCStatusCode

    try:
        return await handle.query("status", rpc_timeout=timedelta(seconds=10))
    except RPCError as error:
        if error.status not in (RPCStatusCode.DEADLINE_EXCEEDED, RPCStatusCode.CANCELLED):
            raise
        return None


async def wait_state(handle: Any, state: str, *, timeout: int = 60) -> None:
    """Waits for a **stable** state, via the `status` query.

    The `after=` parameter used to exist here and was **never read**: `test_spec_refusal` believed
    it checked a pass through `refining` and checked nothing. It is not replaced: rebuilding
    a history by polling would be precisely the same illusion.
    """
    await wait_until(handle, f"state `{state}`", lambda s: s["state"] == state, timeout=timeout)


async def ticket(setup: Fixture) -> Any:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        item = await session.get(WorkItem, setup.work_item_id)
        assert item is not None
        session.expunge(item)
        return item


__all__ = ["pr_ref", "query_status", "runs", "scripted", "start", "ticket", "wait_state", "wait_until"]
