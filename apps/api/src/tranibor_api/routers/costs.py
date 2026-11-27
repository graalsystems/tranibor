# SPDX-License-Identifier: Apache-2.0
"""Costs: aggregates per day, stage, model, backend, size, CSV export.

The DORA metrics (`getProjectDora`) are the `software-delivery` plugin's since round D of ADR 0181: its
extension serves them (`tranibor_software_delivery.api`)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import case, func, or_, select
from tranibor_contracts import StageRole
from tranibor_core import utcnow
from tranibor_core.cross_review import Arm, StageRun, cross_backend_arms, cross_backend_verdict
from tranibor_core.tool_ledger import IN_FLIGHT

from ..db.models import (
    AssistantInstallation,
    CostLedger,
    Finding,
    Organization,
    Project,
    Run,
    WorkItem,
)
from ..deps import Db, Me, ProjectCtx
from ..display_currency import DisplayCurrency
from ..display_currency import read as read_display_currency
from ..errors import forbidden, not_found
from ..rbac import Permission
from ..schemas import (
    CostFx,
    CostReport,
    CostRow,
    CostTotal,
    CrossBackendArm,
    CrossBackendReport,
)
from ..services import effective_policy

router = APIRouter(tags=["costs"])

GROUPS: dict[str, Any] = {
    # `kind` separates what a MODEL cost from what a catalogue TOOL cost. Without
    # it, a third-party API bill melted into the cost of the models and was therefore
    # visible nowhere — the one expense of the platform that nobody looked at.
    "kind": CostLedger.kind,
    "stage": CostLedger.stage_role,
    "model": CostLedger.model,
    "backend": CostLedger.backend,
    "size": CostLedger.size,
    "project": CostLedger.project_id,
    # Who spent it (S29-01, S29-02).
    "agent": CostLedger.agent_slug,
    "workflow": CostLedger.workflow_name,
    "work_item": CostLedger.work_item_id,
    # The door a tool call came through (S57-06, ADR 0122 §4): `model`, `script`, `job`. A line that
    # does not say (a model call, a distillation, a tool line booked before S57-06) is `—`.
    "origin": CostLedger.origin,
    # One run (ADR 0141 §1): named by its work item's key and its stage in `_readable_keys`; a line
    # no run spent (a session, a distillation) is `—`.
    "run": CostLedger.run_id,
    # One assistant installation (ADR 0141 §1): its conversations' turns and its tasks, named by the
    # installation's slug in `_readable_keys`; a line no assistant spent is `—`.
    "assistant": CostLedger.installation_id,
}
#: Every grouping, at project and organisation scope alike, and in the CSV.
GROUP_PATTERN = "^(day|stage|model|backend|size|kind|agent|workflow|work_item|project|origin|run|assistant)$"

#: The filters that narrow a report to one agent, one workflow, one work item, one assistant installation
#: (ADR 0141 §1), validated as the contract declares them.
AgentFilter = Annotated[str | None, Query(pattern="^[a-z0-9][a-z0-9-]{0,63}$")]
WorkflowFilter = Annotated[str | None, Query(min_length=1, max_length=64)]
WorkItemFilter = Annotated[str | None, Query(min_length=1, max_length=128)]
AssistantFilter = Annotated[str | None, Query(min_length=1, max_length=64)]


def _default_since() -> datetime:
    return utcnow() - timedelta(days=30)


async def _report(
    session: Db,
    *,
    where: list[Any],
    group_by: str,
    budget_usd: float | None = None,
) -> CostReport:
    column: Any = GROUPS.get(group_by, CostLedger.project_id)
    if group_by == "day":
        column = func.date(CostLedger.ts)
    query = (
        select(
            column.label("key"),
            func.sum(CostLedger.cost_usd),
            func.sum(CostLedger.cost_eur),
            func.sum(CostLedger.tokens_in),
            func.sum(CostLedger.tokens_out),
            func.sum(CostLedger.tokens_cached),
            func.sum(CostLedger.tokens_cache_write),
            func.sum(CostLedger.tokens_cache_write_1h),
            func.count(CostLedger.id),
            # What an imported demonstration history contributes to the line (ADR 0216): never measured.
            func.sum(case((CostLedger.imported.is_(True), CostLedger.cost_usd), else_=0.0)),
        )
        .where(*where)
        .group_by(column)
        .order_by(column)
    )
    rows = (await session.execute(query)).all()
    report_rows = [
        CostRow(
            key=str(row[0] if row[0] is not None else "—"),
            cost_usd=round(float(row[1] or 0), 6),
            cost_eur=round(float(row[2] or 0), 6),
            tokens_in=int(row[3] or 0),
            tokens_out=int(row[4] or 0),
            tokens_cached=int(row[5] or 0),
            tokens_cache_write=int(row[6] or 0),
            tokens_cache_write_1h=int(row[7] or 0),
            runs=int(row[8] or 0),
            imported_usd=round(float(row[9] or 0), 6),
        )
        for row in rows
    ]
    return CostReport(
        group_by=group_by,
        rows=report_rows,
        total=CostTotal(
            cost_usd=round(sum(r.cost_usd for r in report_rows), 6),
            cost_eur=round(sum(r.cost_eur for r in report_rows), 6),
            budget_usd=budget_usd,
            # The report's tokens, next to its money (ADR 0141 §1): the same rows, summed.
            tokens_in=sum(r.tokens_in for r in report_rows),
            tokens_out=sum(r.tokens_out for r in report_rows),
            tokens_cached=sum(r.tokens_cached for r in report_rows),
            tokens_cache_write=sum(r.tokens_cache_write for r in report_rows),
            tokens_cache_write_1h=sum(r.tokens_cache_write_1h for r in report_rows),
            imported_usd=round(sum(r.imported_usd for r in report_rows), 6),
        ),
    )


def _period(since: date | None, until: date | None, kind: str | None = None) -> list[Any]:
    """The ledger lines of a period: `since` (default 30 days ago) to `until`, both inclusive;
    `kind` keeps only what models cost, or only what tools cost (S29-03). A place held under the
    tool-call cap costs nothing and is not a call yet (S57-03): it is left out of the counts."""
    where: list[Any] = [
        CostLedger.ts >= (datetime.combine(since, datetime.min.time()) if since else _default_since()),
        CostLedger.provider != IN_FLIGHT,
    ]
    if until:
        where.append(CostLedger.ts <= datetime.combine(until, datetime.max.time()))
    if kind:
        where.append(CostLedger.kind == kind)
    return where


def _narrowed(
    agent: str | None, workflow: str | None, work_item: str | None, assistant: str | None
) -> list[Any]:
    """Only the lines of one agent (its slug), one workflow (its name), one work item (its id), one
    assistant installation (its id), each read from the ledger's own columns (ADR 0141 §1); combined,
    a line must match them all."""
    where: list[Any] = []
    if agent is not None:
        where.append(CostLedger.agent_slug == agent)
    if workflow is not None:
        where.append(CostLedger.workflow_name == workflow)
    if work_item is not None:
        where.append(CostLedger.work_item_id == work_item)
    if assistant is not None:
        where.append(CostLedger.installation_id == assistant)
    return where


async def _readable_keys(session: Db, report: CostReport, org_id: str | None) -> CostReport:
    """A project is named by its slug, a work item by its tracker key, a run by its work item's key and
    its stage (`B-1 · implement`, `B-1 · implement #2` from the second attempt), and an assistant by its
    installation's slug, not by their identifiers."""
    if report.group_by == "project":
        query = select(Project.id, Project.slug)
        if org_id is not None:
            query = query.where(Project.org_id == org_id)
        names = {str(r[0]): str(r[1]) for r in (await session.execute(query)).all()}
    elif report.group_by == "work_item":
        ids = [r.key for r in report.rows if r.key != "—"]
        rows = (
            await session.execute(select(WorkItem.id, WorkItem.tracker_key).where(WorkItem.id.in_(ids)))
        ).all()
        names = {str(r[0]): str(r[1]) for r in rows}
    elif report.group_by == "run":
        ids = [r.key for r in report.rows if r.key != "—"]
        runs = (
            await session.execute(
                select(Run.id, WorkItem.tracker_key, Run.stage_role, Run.attempt)
                .join(WorkItem, WorkItem.id == Run.work_item_id)
                .where(Run.id.in_(ids))
            )
        ).all()
        names = {
            str(run_id): f"{key} · {stage}" + (f" #{attempt}" if (attempt or 1) > 1 else "")
            for run_id, key, stage, attempt in runs
        }
    elif report.group_by == "assistant":
        ids = [r.key for r in report.rows if r.key != "—"]
        rows = (
            await session.execute(
                select(AssistantInstallation.id, AssistantInstallation.slug).where(
                    AssistantInstallation.id.in_(ids)
                )
            )
        ).all()
        names = {str(r[0]): str(r[1]) for r in rows}
    else:
        return report
    for row in report.rows:
        row.key = names.get(row.key, row.key)
    return report


def _show_in(report: CostReport, display: DisplayCurrency) -> None:
    """Say which currency the report is read in and fill `cost_display` (ADR 0214). The USD and EUR
    figures stay as measured; a declared rate only ever produces the extra column, and the report says so."""
    report.currency = display.code
    if display.declared:
        report.fx = CostFx(declared=True, rate=display.rate, set_on=display.set_on)
    lines: list[CostRow | CostTotal] = [*report.rows, report.total]
    for line in lines:
        if display.declared:
            line.cost_display = display.convert(line.cost_usd)
        else:
            line.cost_display = line.cost_usd if display.code == "USD" else line.cost_eur


@router.get("/projects/{id}/costs", response_model=CostReport, operation_id="getProjectCosts")
async def project_costs(  # noqa: PLR0913 - the report's period and filters, as query parameters
    ctx: ProjectCtx,
    session: Db,
    group_by: Annotated[str, Query(pattern=GROUP_PATTERN)] = "day",
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    kind: Annotated[str | None, Query(pattern="^(model|tool|distill)$")] = None,
    agent: AgentFilter = None,
    workflow: WorkflowFilter = None,
    work_item: WorkItemFilter = None,
    assistant: AssistantFilter = None,
) -> CostReport:
    where: list[Any] = [
        CostLedger.project_id == ctx.id,
        *_period(since, until, kind),
        *_narrowed(agent, workflow, work_item, assistant),
    ]
    policy = await effective_policy(session, ctx.id)
    report = await _report(
        session, where=where, group_by=group_by, budget_usd=policy.budgets.daily_project_usd
    )
    organisation = await session.get(Organization, ctx.project.org_id)
    _show_in(report, read_display_currency(organisation.settings if organisation is not None else None))
    return await _readable_keys(session, report, None)


@router.get("/orgs/{org}/costs", response_model=CostReport, operation_id="getOrgCosts")
async def org_costs(  # noqa: PLR0913 - the report's period and filters, as query parameters
    org: str,
    session: Db,
    principal: Me,
    group_by: Annotated[str, Query(pattern=GROUP_PATTERN)] = "day",
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    kind: Annotated[str | None, Query(pattern="^(model|tool|distill)$")] = None,
    agent: AgentFilter = None,
    workflow: WorkflowFilter = None,
    work_item: WorkItemFilter = None,
    assistant: AssistantFilter = None,
) -> CostReport:
    organization = (
        await session.execute(select(Organization).where(Organization.slug == org))
    ).scalar_one_or_none()
    if organization is None:
        raise not_found("Organisation", org)
    if not principal.can(Permission.PROJECT_READ, org):
        raise forbidden()
    project_ids = (
        (await session.execute(select(Project.id).where(Project.org_id == organization.id))).scalars().all()
    )
    # The organisation's projects' lines, and its own (a distillation of a base with no owning
    # project, ADR 0092): the lines with no project are only ever visible here.
    where: list[Any] = [
        or_(CostLedger.project_id.in_(list(project_ids) or [""]), CostLedger.org_id == organization.id),
        *_period(since, until, kind),
        *_narrowed(agent, workflow, work_item, assistant),
    ]
    report = await _report(session, where=where, group_by=group_by)
    _show_in(report, read_display_currency(organization.settings))
    # The organisation's daily budget: what its projects may spend a day, summed (S29-03).
    daily = [(await effective_policy(session, pid)).budgets.daily_project_usd for pid in project_ids]
    budgets = [b for b in daily if b is not None]
    report.total.budget_usd = round(sum(budgets), 6) if budgets else None
    return await _readable_keys(session, report, organization.id)


@router.get(
    "/projects/{id}/costs.csv",
    operation_id="exportProjectCostsCsv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "Cost report as CSV"}},
)
async def project_costs_csv(  # noqa: PLR0913 - the report's period and filters, as query parameters
    ctx: ProjectCtx,
    session: Db,
    group_by: Annotated[str, Query(pattern=GROUP_PATTERN)] = "day",
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    kind: Annotated[str | None, Query(pattern="^(model|tool|distill)$")] = None,
    agent: AgentFilter = None,
    workflow: WorkflowFilter = None,
    work_item: WorkItemFilter = None,
    assistant: AssistantFilter = None,
) -> Response:
    """The same report, as CSV: something to paste into a spreadsheet or an invoice."""
    report = await project_costs(
        ctx,
        session,
        group_by=group_by,
        since=since,
        until=until,
        kind=kind,
        agent=agent,
        workflow=workflow,
        work_item=work_item,
        assistant=assistant,
    )
    return _csv_response(report, f"couts-{ctx.slug}-{group_by}-{utcnow():%Y%m%d}.csv")


@router.get(
    "/orgs/{org}/costs.csv",
    operation_id="exportOrgCostsCsv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "Organisation cost report as CSV"}},
)
async def org_costs_csv(  # noqa: PLR0913 - the report's period and filters, as query parameters
    org: str,
    session: Db,
    principal: Me,
    group_by: Annotated[str, Query(pattern=GROUP_PATTERN)] = "day",
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    kind: Annotated[str | None, Query(pattern="^(model|tool|distill)$")] = None,
    agent: AgentFilter = None,
    workflow: WorkflowFilter = None,
    work_item: WorkItemFilter = None,
    assistant: AssistantFilter = None,
) -> Response:
    """The organisation's report, in the grouping the screen shows."""
    report = await org_costs(
        org,
        session,
        principal,
        group_by=group_by,
        since=since,
        until=until,
        kind=kind,
        agent=agent,
        workflow=workflow,
        work_item=work_item,
        assistant=assistant,
    )
    return _csv_response(report, f"couts-{org}-{group_by}-{utcnow():%Y%m%d}.csv")


def _csv_response(report: CostReport, filename: str) -> Response:
    group_by = report.group_by
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            group_by,
            "cout_usd",
            "cout_eur",
            "tokens_in",
            "tokens_out",
            "tokens_caches",
            "runs",
            "tokens_cache_write",
            *([f"cost_{report.currency.lower()}"] if report.fx.declared else []),
            # What a demonstration history import put in the line (ADR 0216): in `cout_usd`, never measured.
            "imported_usd",
        ]
    )
    for row in report.rows:
        writer.writerow(
            [
                row.key,
                f"{row.cost_usd:.6f}",
                f"{row.cost_eur:.6f}",
                row.tokens_in,
                row.tokens_out,
                row.tokens_cached,
                row.runs,
                row.tokens_cache_write,
                *([f"{row.cost_display:.6f}"] if report.fx.declared else []),
                f"{row.imported_usd:.6f}",
            ]
        )
    writer.writerow([])
    total = report.total
    writer.writerow(
        [
            "total",
            f"{total.cost_usd:.6f}",
            f"{total.cost_eur:.6f}",
            total.tokens_in,
            total.tokens_out,
            total.tokens_cached,
            "",  # a total has no runs cell: the write column keeps its position
            total.tokens_cache_write,
            *([f"{total.cost_display:.6f}"] if report.fx.declared else []),
            f"{total.imported_usd:.6f}",
        ]
    )
    if report.fx.declared:
        # Said in the file itself, so a figure pasted into a spreadsheet keeps its provenance (ADR 0214).
        writer.writerow(["declared_rate", f"1 USD = {report.fx.rate} {report.currency}", report.fx.set_on])
        writer.writerow(
            [
                "note",
                f"cost_{report.currency.lower()} is cost_usd (as measured) converted at the rate the "
                "organisation declared; the USD figures are the ledger's",
            ]
        )
    if total.budget_usd is not None:
        writer.writerow(["budget_quotidien_usd", f"{total.budget_usd:.2f}"])
    return Response(
        # French-speaking Excel reads the semicolon; the BOM keeps it from breaking the accents.
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ───────────────────── cross review: does multi-backend pay off? ─────────────────────


@router.get(
    "/projects/{id}/metrics/cross-backend",
    response_model=CrossBackendReport,
    operation_id="getProjectCrossBackend",
)
async def project_cross_backend(
    ctx: ProjectCtx,
    session: Db,
    since: Annotated[date | None, Query()] = None,
) -> CrossBackendReport:
    """Compare the reviews made by another backend with those made by the same one (S13-03).

    "A reviewer that is not the implementer" is a policy; knowing whether it is worth anything takes
    a measure. Per arm, it counts the reviews that found something: a finding filed, or a result
    other than `done`. What counts as a cross review, who the implementer was when the review was
    made and the verdict are defined once in `tranibor_core.cross_review`, the same definitions the
    orchestrator's reviewer swap uses.
    """
    start = datetime.combine(since, datetime.min.time(), tzinfo=UTC) if since else _default_since()
    reviews = (
        (
            await session.execute(
                select(
                    Run.id, Run.work_item_id, Run.stage_role, Run.backend, Run.created_at, Run.result
                ).where(
                    Run.project_id == ctx.id,
                    Run.stage_role == str(StageRole.REVIEW),
                    Run.created_at >= start,
                )
            )
        )
        .tuples()
        .all()
    )
    # Every implement run, not only those since `start`: a review at the start of the window
    # compares with the implementer who came before it.
    implements = (
        (
            await session.execute(
                select(Run.id, Run.work_item_id, Run.stage_role, Run.backend, Run.created_at).where(
                    Run.project_id == ctx.id,
                    Run.stage_role == str(StageRole.IMPLEMENT),
                    Run.backend.is_not(None),
                )
            )
        )
        .tuples()
        .all()
    )
    with_findings = set(
        (
            await session.execute(
                select(Finding.origin_run_id).where(
                    Finding.project_id == ctx.id, Finding.origin_run_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )

    stage_runs = [
        StageRun(
            run_id=run_id,
            work_item_id=str(work_item_id),
            role=role,
            backend=backend,
            created_at=created_at,
            caught=run_id in with_findings or (result or {}).get("status") not in {None, "done"},
        )
        for run_id, work_item_id, role, backend, created_at, result in reviews
    ]
    stage_runs += [
        StageRun(
            run_id=run_id, work_item_id=str(work_item_id), role=role, backend=backend, created_at=created_at
        )
        for run_id, work_item_id, role, backend, created_at in implements
    ]
    same, other = cross_backend_arms(stage_runs)

    def to_arm(arm: Arm) -> CrossBackendArm:
        return CrossBackendArm(
            reviews=arm.reviews,
            caught=arm.caught,
            catch_rate=arm.catch_rate,
            backends=sorted(arm.backends),
        )

    policy = await effective_policy(session, ctx.id)
    verdict, detail = cross_backend_verdict(same, other)
    return CrossBackendReport(
        since=start,
        cross_backend_required=policy.review.cross_backend,
        same_backend=to_arm(same),
        other_backend=to_arm(other),
        verdict=verdict,
        detail=detail,
    )
