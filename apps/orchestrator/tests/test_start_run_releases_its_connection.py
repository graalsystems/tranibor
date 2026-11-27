"""`start_run` holds no pooled connection across the executor's call (#2311).

It opened its session, read the project and the run, minted the run's key, then awaited
`executor.start(spec)` with its transaction (and connection) still held: on PostgreSQL a connection
`idle in transaction` for as long as Kubernetes, Tekton or a cloud took to answer, the pattern #1354
removed from the API's tool calls. Now the session commits and hands its connection back before the
call. The run's key and its row are committed with it, so a start retried after the executor failed
must find them: one reservation for the run, never two, and the first key revoked.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Fixture
from .test_gateway_key_through_the_secret import _key_rows, _open_reservations, _plan, _run

pytestmark = pytest.mark.asyncio


def _start_payload(setup: Fixture, prepared: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": setup.project_id,
        "work_item_id": setup.work_item_id,
        "stage_input": prepared["stage_input"],
    }


async def test_the_executor_is_called_with_no_connection_checked_out(
    setup: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.db.session import get_engine
    from tranibor_orchestrator.activities import stage as activities

    executor = setup.adapters.executor
    real = executor.start
    held: list[int] = []

    async def watching(spec: Any) -> Any:
        held.append(get_engine().pool.checkedout())
        return await real(spec)

    monkeypatch.setattr(executor, "start", watching)
    prepared = await activities.prepare_stage(_plan(setup))
    started = await activities.start_run(_start_payload(setup, prepared))

    assert held == [0], "start_run held a pooled connection across the executor's call"
    run = await _run(prepared["run_id"])
    assert run.executor_ref == started["executor_ref"] and run.status == "running", (
        "what follows the call is written in a new transaction"
    )


async def test_a_start_retried_after_the_executor_failed_holds_one_reservation(
    setup: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_orchestrator.activities import stage as activities

    executor = setup.adapters.executor
    gateway = setup.adapters.gateway
    real = executor.start
    calls = 0

    async def failing_once(spec: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("the cluster did not answer")
        return await real(spec)

    monkeypatch.setattr(executor, "start", failing_once)
    prepared = await activities.prepare_stage(_plan(setup))
    budget = prepared["stage_input"]["budget"]["usd"]
    with pytest.raises(ConnectionError):
        await activities.start_run(_start_payload(setup, prepared))
    [first] = gateway.keys
    assert await _key_rows(setup) == {first: (False, pytest.approx(budget))}, "the first key's row, committed"

    await activities.start_run(_start_payload(setup, prepared))  # Temporal's retry

    second = next(key for key in gateway.keys if key != first)
    assert await _key_rows(setup) == {second: (False, pytest.approx(budget))}, (
        "the run's row became the second key's: one reservation, not two"
    )
    assert await _open_reservations(setup) == pytest.approx(budget)
    assert first in gateway.revoked, "the first attempt's key is revoked, not left to its TTL"
    assert (await _run(prepared["run_id"])).gateway_key_id == second
