# SPDX-License-Identifier: Apache-2.0
"""The software-delivery reads of the API (ADR 0181, round D): a run's diff (`getRunDiff`) and a project's
DORA metrics (`getProjectDora`), served by the plugin's extension through `extensions.declare_router` under
the paths and operation ids `openapi.yaml` marks `x-plugin: software-delivery`. The core's routers held
them until round D; the core's dependencies guard them as they guard the core's own routes.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from tranibor_api.db.models import Deployment, Project, Release, Run, WorkItem
from tranibor_api.deps import Db, Me, ProjectCtx, resolve_project
from tranibor_api.errors import forbidden, not_found
from tranibor_api.knowledge.marked import readable
from tranibor_api.rbac import Permission
from tranibor_api.schemas import DiffFileDto, DiffSummaryDto, DoraMetric, DoraReport
from tranibor_core import aware, matches_any, utcnow

#: The run's diff keeps the `runs` tag and the DORA metrics the `costs` tag, as the contract tags them.
router = APIRouter()


async def _run(session: Any, run_id: str, principal: Any) -> tuple[Run, Project]:
    """The run, as the core's runs routes read it: refused to a caller who may not read its project, and
    absent to one who may not read its marked work item (ADR 0114)."""
    run = await session.get(Run, run_id)
    if run is None:
        raise not_found("Run", run_id)
    project = await session.get(Project, run.project_id)
    if project is None:
        raise not_found("Project", run.project_id)
    _, org_slug = await resolve_project(session, project.id)
    if not principal.can(Permission.PROJECT_READ, org_slug, project.slug):
        raise forbidden()
    item = await session.get(WorkItem, run.work_item_id)
    if item is not None and not await readable(session, principal, item, project.org_id):
        raise not_found("Run", run_id)
    return run, project


@router.get("/runs/{id}/diff", response_model=DiffSummaryDto, operation_id="getRunDiff", tags=["runs"])
async def diff(id: str, session: Db, principal: Me) -> DiffSummaryDto:
    """The run's diff, annotated: each file is marked inside or outside the allowed scope."""
    run, _project = await _run(session, id, principal)
    result = run.result or {}
    reports: dict[str, Any] = (result.get("artifacts") or {}).get("reports", {})
    raw = reports.get("diff_files")
    files_raw: list[dict[str, Any]] = json.loads(raw) if isinstance(raw, str) else (raw or [])
    allowed = list(run.allowed_paths or [])
    files = [
        DiffFileDto(
            path=entry.get("path", ""),
            status=entry.get("status", "modified"),
            additions=int(entry.get("additions", 0)),
            deletions=int(entry.get("deletions", 0)),
            patch=entry.get("patch"),
            in_scope=matches_any(entry.get("path", ""), allowed) if allowed else True,
        )
        for entry in files_raw
    ]
    return DiffSummaryDto(
        base=reports.get("base"),
        head=(((result.get("artifacts") or {}).get("delivery")) or {}).get("branch"),
        files=files,
        additions=sum(f.additions for f in files),
        deletions=sum(f.deletions for f in files),
    )


FREQUENCY_LEVELS = ((1.0, "elite"), (1 / 7, "high"), (1 / 30, "medium"))
LEAD_TIME_LEVELS = ((24.0, "elite"), (24.0 * 7, "high"), (24.0 * 30, "medium"))
RESTORE_LEVELS = ((1.0, "elite"), (24.0, "high"), (24.0 * 7, "medium"))
FAILURE_LEVELS = ((0.05, "elite"), (0.10, "high"), (0.15, "medium"))


def _level(value: float | None, thresholds: tuple[tuple[float, str], ...], *, higher_is_better: bool) -> str:
    if value is None:
        return "unknown"
    for limit, name in thresholds:
        if (value >= limit) if higher_is_better else (value <= limit):
            return name
    return "low"


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    """Gap in hours, robust to the naive timestamps SQLite returns."""
    if start is None or end is None:
        return None
    from tranibor_core import elapsed_seconds

    delta = elapsed_seconds(start, end) / 3600
    return delta if delta >= 0 else None


@router.get(
    "/projects/{id}/metrics/dora", response_model=DoraReport, operation_id="getProjectDora", tags=["costs"]
)
async def project_dora(
    ctx: ProjectCtx,
    session: Db,
    env: Annotated[str, Query(pattern="^[a-z0-9-]{1,32}$")] = "prod",
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
) -> DoraReport:
    """The four DORA measures, derived from the recorded deployments and rollbacks.

    Nothing is declared by hand: the frequency comes from the closed `deployments` rows,
    the lead time from the gap between a ticket's merge and the production release
    of the batch that carried it, the failure rate from the rollbacks, and the recovery time
    from the gap between a rollback and the successful deployment that followed it.
    """
    # Both bounds are timezone-aware: SQLite returns naive timestamps and
    # subtracting them from an aware bound would raise a TypeError in the middle of the request.
    start = (
        datetime.combine(since, datetime.min.time(), tzinfo=UTC) if since else utcnow() - timedelta(days=30)
    )
    end = datetime.combine(until, datetime.max.time(), tzinfo=UTC) if until else utcnow()
    rows = (
        (
            await session.execute(
                select(Deployment, Release)
                .join(Release, Deployment.release_id == Release.id)
                .where(
                    Release.project_id == ctx.id,
                    Deployment.env == env,
                    Deployment.started_at >= start,
                    Deployment.started_at <= end,
                )
                .order_by(Deployment.started_at)
            )
        )
        .tuples()
        .all()
    )
    succeeded = [(d, r) for d, r in rows if d.status == "succeeded"]
    failed = [(d, r) for d, r in rows if d.status == "rolled_back"]
    finished = succeeded + failed

    days = max(1.0, ((end - start).total_seconds() / 86400))
    frequency = len(succeeded) / days

    lead_times: list[float] = []
    for deployment, release in succeeded:
        for item in release.items or []:
            merged_at = item.get("merged_at")
            if not merged_at:
                continue
            hours = _hours(datetime.fromisoformat(str(merged_at)), deployment.ended_at)
            if hours is not None:
                lead_times.append(hours)

    restores: list[float] = []
    ordered = sorted(finished, key=lambda pair: aware(pair[0].started_at) or start)
    for index, (deployment, _release) in enumerate(ordered):
        if deployment.status != "rolled_back":
            continue
        following = next((d for d, _ in ordered[index + 1 :] if d.status == "succeeded"), None)
        hours = _hours(deployment.ended_at, following.ended_at) if following else None
        if hours is not None:
            restores.append(hours)

    failure_rate = (len(failed) / len(finished)) if finished else None
    lead_p50 = median(lead_times) if lead_times else None
    restore_p50 = median(restores) if restores else None
    return DoraReport(
        env=env,
        since=start,
        until=end,
        deployments=len(succeeded),
        deployment_frequency=DoraMetric(
            value=round(frequency, 4),
            unit="par jour",
            level=_level(frequency, FREQUENCY_LEVELS, higher_is_better=True) if succeeded else "unknown",
            sample=len(succeeded),
        ),
        lead_time=DoraMetric(
            value=round(lead_p50, 2) if lead_p50 is not None else None,
            unit="hours (median)",
            level=_level(lead_p50, LEAD_TIME_LEVELS, higher_is_better=False),
            sample=len(lead_times),
        ),
        change_failure_rate=DoraMetric(
            value=round(failure_rate, 4) if failure_rate is not None else None,
            unit="share of production releases",
            level=_level(failure_rate, FAILURE_LEVELS, higher_is_better=False),
            sample=len(finished),
        ),
        time_to_restore=DoraMetric(
            value=round(restore_p50, 2) if restore_p50 is not None else None,
            unit="hours (median)",
            level=_level(restore_p50, RESTORE_LEVELS, higher_is_better=False),
            sample=len(restores),
        ),
    )
