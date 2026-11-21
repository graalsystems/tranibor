# SPDX-License-Identifier: Apache-2.0
"""The inline demo worker: the orchestrator's workers inside the API's process (ADR 0212).

`TRANIBOR_DEMO_INLINE_WORKER=1`, under `TRANIBOR_FAKES=1` and `TRANIBOR_ENV=dev` (or `test`), makes the
API start an in-memory Temporal test server and the **same** workers the chart deploys (the
orchestrator's `build_workers`, one per queue), and route its workflow starts and signals to them.
A decision posted to the API alone then moves the work item along its real workflow, with the simulated
agent of the fakes, which is what a presentation on a laptop needs.

It is a presentation aid, not a mode of the product: no durability (everything dies with the process),
no real agent, tracker or repository, and it refuses to start anywhere else.
"""

from __future__ import annotations

from typing import Any

from tranibor_core.production import refuse_demo_inline_worker

from .config import Settings
from .logging import get_logger
from .temporal import RealTemporal, set_temporal

logger = get_logger("choregos.demo_inline")


class _Board(dict[str, Any]):
    """The fake tracker's items, which know any ticket the API creates.

    The fake tracker is scripted by `seed()`; here the tickets come from the API, so a key the fake never
    heard of is taken up as a ticket in `Todo` rather than raising `KeyError` at the first state mirror."""

    def __missing__(self, key: str) -> Any:
        from tranibor_core.domain import WorkItemData

        item = WorkItemData(key=key, title=key, state="Todo")
        self[key] = item
        return item


class InlineTemporal(RealTemporal):
    """The real gateway (same starts, same signals, same id policies), on the in-process server's client."""

    def __init__(self, client: Any, task_queue: str) -> None:
        super().__init__("in-process", "default", task_queue)
        self._client = client


class InlineWorker:
    """The server and the workers, started together and stopped together."""

    def __init__(self, environment: Any, workers: list[Any]) -> None:
        self.environment = environment
        self.workers = workers
        self._started: list[Any] = []

    async def start(self) -> None:
        for worker in self.workers:
            await worker.__aenter__()
            self._started.append(worker)

    async def stop(self) -> None:
        while self._started:
            worker = self._started.pop()
            await worker.__aexit__(None, None, None)
        await self.environment.shutdown()


async def start_inline_worker(settings: Settings) -> InlineWorker:
    """Refuse outside development under the fakes; otherwise start and wire the worker in."""
    refuse_demo_inline_worker(environment=settings.env, fakes=settings.fakes)
    try:
        from temporalio.testing import WorkflowEnvironment
        from tranibor_adapters import AdapterSet
        from tranibor_orchestrator.activities.base import set_adapters_override
        from tranibor_orchestrator.config import ALL_QUEUES
        from tranibor_orchestrator.config import get_settings as orchestrator_settings
        from tranibor_orchestrator.worker import build_workers
    except ImportError as exc:  # the `api` image carries no orchestrator
        raise RuntimeError(
            "TRANIBOR_DEMO_INLINE_WORKER needs the orchestrator package in the API's environment "
            f"(`uv run` from the repository has it): {exc}"
        ) from exc

    # One shared set of fakes, as `make demo` has: the agent's results, the tracker and the repository
    # must be the same objects from one activity to the next.
    adapters = AdapterSet.fakes()
    adapters.tracker.items = _Board()  # type: ignore[attr-defined]
    set_adapters_override(adapters)
    environment = await WorkflowEnvironment.start_time_skipping()
    workers = build_workers(environment.client, list(ALL_QUEUES), orchestrator_settings())
    inline = InlineWorker(environment, workers)
    await inline.start()
    set_temporal(InlineTemporal(environment.client, settings.temporal_task_queue))
    logger.warning(
        "inline demo worker running: workflows run in this process, on an in-memory Temporal "
        "server; nothing survives a restart (ADR 0212)",
        queues=list(ALL_QUEUES),
    )
    return inline


async def stop_inline_worker(inline: InlineWorker) -> None:
    from tranibor_orchestrator.activities.base import set_adapters_override

    set_temporal(None)
    await inline.stop()
    set_adapters_override(None)
