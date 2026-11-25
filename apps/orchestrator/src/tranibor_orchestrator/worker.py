# SPDX-License-Identifier: Apache-2.0
"""Temporal workers: one deployment per task queue (docs/plan/02 §2.1)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from typing import Any, TypedDict

from tranibor_api.logging import configure_logging, get_logger
from tranibor_core import telemetry

from .config import ALL_QUEUES, OrchestratorSettings, get_settings
from .extension_parts import all_activities, all_workflows
from .queues import ACTIVITIES_BY_QUEUE

logger = get_logger("choregos.worker")


def activities_for(queue: str) -> list[Any]:
    """The `orchestrator` queue carries everything; the others carry their specialty.

    The net is deliberate: the workflows now schedule each activity on its own queue
    (`workflows.scheduling`), but a task already waiting on `orchestrator` at the time of a version
    upgrade must not be left orphaned. The split is guaranteed by a test, not by an absence of
    registration. The extensions' activities (the worker seam, ADR 0181 round D) are in it, placed on
    their queues once `all_activities` resolved them.
    """
    everything = all_activities()
    if queue == "orchestrator":
        return everything
    names = ACTIVITIES_BY_QUEUE.get(queue, ())
    selected: list[Any] = []
    for candidate in everything:
        definition = getattr(candidate, "__temporal_activity_definition", None)
        if definition is not None and getattr(definition, "name", "") in names:
            selected.append(candidate)
    return selected


async def start_reconciliation_loops() -> list[str]:
    """One catch-up loop per active project (S3-06).

    The start is idempotent: restarting a worker does not create a second loop, and a project added
    afterwards is picked up at the next restart or by the API.
    """
    from tranibor_api.services.projects import active_projects
    from tranibor_core.temporal_connection import organisation_scope

    from .train_client import start_reconciliation_loop

    settings = get_settings()
    if settings.reconcile_interval_seconds <= 0:
        logger.info("tracker catch-up disabled")
        return []
    slugs = await active_projects()
    started: list[str] = []
    for slug, org_id in slugs:
        # The loop's payloads under its organisation's key (ADR 0224).
        with organisation_scope(org_id):
            fresh = await start_reconciliation_loop(slug)
        if fresh:
            started.append(slug)
    logger.info("tracker catch-up", projects=len(slugs), started=len(started))
    return started


async def start_memory_loops() -> list[str]:
    """One memory loop per active project whose policy keeps memory (S51-02).

    Idempotent like the tracker loops: restarting a worker does not create a second loop, and a loop
    that died in failure starts again. A project whose policy says `memory.enabled: false` gets none.
    """
    from tranibor_api.services.projects import active_projects
    from tranibor_core.temporal_connection import organisation_scope

    from .train_client import start_memory_loop

    slugs = await active_projects(keeping_memory=True)
    started: list[str] = []
    for slug, org_id in slugs:
        with organisation_scope(org_id):
            fresh = await start_memory_loop(slug)
        if fresh:
            started.append(slug)
    logger.info("memory loops", projects=len(slugs), started=len(started))
    return started


async def start_knowledge_sync_loops() -> list[str]:
    """One sync loop per knowledge source (S33-04). Idempotent like the tracker loops: the API
    starts a source's loop when it creates it; the worker restarts the ones a failure ended."""
    from sqlalchemy import select
    from tranibor_api.db.models import KnowledgeSource, Organization
    from tranibor_api.db.session import session_scope
    from tranibor_api.temporal import knowledge_sync_id
    from tranibor_core.temporal_connection import organisation_scope

    from .train_client import start_workflow_once

    async with session_scope(orgs="*") as session:
        rows = (
            await session.execute(
                select(
                    Organization.slug,
                    KnowledgeSource.id,
                    KnowledgeSource.interval_minutes,
                    KnowledgeSource.org_id,
                ).join(Organization, Organization.id == KnowledgeSource.org_id)
            )
        ).tuples()
        sources = list(rows)
    started: list[str] = []
    for org, source_id, minutes, org_id in sources:
        with organisation_scope(org_id):
            fresh = await start_workflow_once(
                "KnowledgeSync",
                knowledge_sync_id(source_id),
                {"org": org, "source_id": source_id, "interval_seconds": minutes * 60},
                relancer_si_echoue=True,  # lang: fr-ok (the keyword test_reconciliation pins)
            )
        if fresh:
            started.append(source_id)
    logger.info("knowledge sync loops", sources=len(sources), started=len(started))
    return started


class ConcurrencyOptions(TypedDict):
    max_concurrent_activities: int
    max_concurrent_workflow_tasks: int
    max_concurrent_local_activities: int
    max_cached_workflows: int


def concurrency_options(settings: OrchestratorSettings) -> ConcurrencyOptions:
    """The `Worker` keywords that bound one process's load (S80-07a): settings, not literals."""
    return {
        "max_concurrent_activities": settings.worker_max_concurrent_activities,
        "max_concurrent_workflow_tasks": settings.worker_max_concurrent_workflow_tasks,
        "max_concurrent_local_activities": settings.worker_max_concurrent_local_activities,
        "max_cached_workflows": settings.worker_max_cached_workflows,
    }


def build_workers(client: Any, queues: list[str], settings: OrchestratorSettings) -> list[Any]:
    """One `Worker` per queue, registered as the chart's deployments are (shared with the inline demo
    worker of ADR 0212, so that a demo never registers anything the product does not)."""
    from temporalio.worker import Worker

    # The core's workflows and the extensions' (the worker seam): a clash of names stops the start here.
    workflows = all_workflows()
    return [
        Worker(
            client,
            task_queue=queue,
            workflows=workflows if queue == "orchestrator" else [],
            activities=activities_for(queue),
            **concurrency_options(settings),
        )
        for queue in queues
    ]


async def run_worker(queues: list[str]) -> None:
    from tranibor_adapters import load_extensions
    from tranibor_core.temporal_connection import TemporalSecurity, connect_temporal

    # Extensions also declare EFFECTS of governed actions (ADR 0035): the API knows them in order
    # to propose, and the worker must know them in order to PLAY them. It did not load them, so an
    # extension's effect was "unknown" in the very place where it had to run (S20-08).
    load_extensions()  # lang: fr-ok (renamed by S45-10)
    settings = get_settings()
    # Before anything connects: no fakes in staging and prod (S80-03).
    settings.refuse_fakes_in_production()
    # An organisation's payloads under its own data key, read through the deployment's key wrapper
    # (ADR 0224): installed before the converter is built, so every client of this process carries it.
    from tranibor_api.services.temporal_keys import install_organisation_data_keys

    data_keys = install_organisation_data_keys()
    # The workers decode what the clients encode: one converter for the whole deployment (ADR 0183).
    security = TemporalSecurity.from_env()
    # No clear payloads in staging and prod unless the operator said so (ADR 0220).
    security.refuse_clear_in_production(settings.env)
    client = await connect_temporal(settings.temporal_address, settings.temporal_namespace, security=security)
    workers = build_workers(client, queues, settings)
    logger.info(
        "workers started",
        queues=queues,
        address=settings.temporal_address,
        **concurrency_options(settings),
        tls=security.tls,
        payload_codec=security.encrypts,
        organisation_data_keys=data_keys is not None,
    )
    async with contextlib.AsyncExitStack() as stack:
        for worker in workers:
            await stack.enter_async_context(worker)
        if "orchestrator" in queues:
            await start_reconciliation_loops()
            await start_memory_loops()
            await start_knowledge_sync_loops()
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="Workers Temporal de Tranibor")
    parser.add_argument(
        "--queues",
        default=",".join(ALL_QUEUES),
        help=f"comma-separated task queues (default: {','.join(ALL_QUEUES)})",
    )
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    telemetry.configure_tracing("choregos-orchestrator")
    queues = [queue.strip() for queue in args.queues.split(",") if queue.strip()]
    try:
        asyncio.run(run_worker(queues))
    finally:
        telemetry.shutdown_tracing()


if __name__ == "__main__":
    main()
