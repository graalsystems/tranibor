# SPDX-License-Identifier: Apache-2.0
"""A demonstration history, taken in through the API (ADR 0216).

A pre-sales demonstration needs a project that looks lived in: work items that went through their states,
runs with their tokens and cost, decisions people took, findings raised. Nothing in the API writes a
past, so a demo used to open the database. This module takes a declared document (`DemoHistory`) and
writes it through the same builders a live ticket goes through (`new_work_item`, `new_human_request`, the
workflow's own states and field validation), then marks every row `imported`.

- **Everything is checked before anything is written** (`_plan`): states against the workflow, people
  against the organisation's members, agents against its registry, models against the declared prices,
  ids for duplicates. A refusal is a 422 naming the object.
- **Idempotent**: every row's id derives from the project and the document's external id, and the work
  item keeps the digest of what it was imported from. The same document again writes nothing; an id that
  already holds other content is a 409, never a silent overwrite.
- **Never mistaken for measured**: `imported` on the work item, its runs, its findings and its cost lines;
  `imported_usd` in every cost report; no budget and no agent metric counts an imported line.
- **A request may still wait** (`pending`): it is the same row a live step writes, so the inbox lists it
  and the decision route decides it; the route passes the decision of an imported work item to no workflow
  (`routers/workitems.py`), and refuses every control on it, so no interpreter ever starts for it.
- **Removable** (`remove_history`): the marked rows of a project, and nothing else.

The gate (`TRANIBOR_DEMO_HISTORY_IMPORT`, never in production) is the router's, `tranibor_core.production`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import (
    DemoHistory,
    DemoPrice,
    DemoRun,
    DemoWorkItem,
    EventType,
    HumanDecision,
    HumanRequestKind,
    Workflow,
)
from tranibor_core import WorkflowEngine
from tranibor_core.finding_types import refusal as finding_type_refusal

from ..audit import record
from ..config import get_settings
from ..db.models import (
    Agent,
    CostLedger,
    Event,
    Finding,
    HumanRequest,
    Membership,
    Project,
    Run,
    User,
    WorkItem,
)
from ..errors import conflict, unprocessable
from ..rbac import Principal
from ..schemas import DemoHistoryCounts, DemoHistoryImport, DemoHistoryRemoval
from .definitions import workflow_model
from .human_requests import new_human_request
from .initiator import initiator
from .routing import WorkItemBirth, choose_workflow, new_work_item, validate_fields, verify_birth

#: Where a derived id comes from: the project and the document's own ids, so a second import finds the first.
_NAMESPACE = uuid.UUID("5b0f0f0e-6d54-4a53-9a7e-0d6a0a4c3a16")
#: The key of an imported work item; the colon keeps it clear of every key a tracker or the platform mints.
KEY_PREFIX = "demo:"
#: The marker kept in `work_items.documents`, with the digest of what the work item was imported from.
MARKER = "demo_import"


def derived_id(project_id: str, *parts: str) -> str:
    """A stable id for an object of the document: the same document gives the same ids (ADR 0008)."""
    return str(uuid.uuid5(_NAMESPACE, "/".join((project_id, *parts))))


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _mtok(tokens: int, price: float) -> float:
    return tokens / 1_000_000 * price


def cost_of(run: DemoRun, prices: dict[str, DemoPrice]) -> float:
    """What the run cost: the declared figure, else its tokens at the declared price of its model."""
    if run.cost_usd is not None:
        return round(run.cost_usd, 6)
    if run.tokens is None:
        return 0.0
    price = prices.get(run.model or "")
    if price is None:
        raise unprocessable(
            f"run `{run.id}` has tokens and no `cost_usd`, and `prices` declares nothing for the model "
            f"`{run.model or '(none)'}`: declare its price, or give the run's `cost_usd`"
        )
    cached = run.tokens.cached
    cached_price = price.cached_input_usd_per_mtok
    cached_price = price.input_usd_per_mtok if cached_price is None else cached_price
    return round(
        _mtok(run.tokens.input - cached, price.input_usd_per_mtok)
        + _mtok(cached, cached_price)
        + _mtok(run.tokens.output, price.output_usd_per_mtok),
        6,
    )


def digest_of(item: DemoWorkItem, workflow: str, prices: dict[str, DemoPrice]) -> str:
    """What an import of this work item stands for: its content, the workflow and the prices of its models."""
    used = {run.model: prices[run.model].model_dump() for run in item.runs if run.model in prices}
    dumped = item.model_dump(mode="json")
    if dumped.get("pending") is None:
        # A work item without a waiting request keeps the digest it had before `pending` existed: a document
        # imported then is still the same document now, not a 409.
        dumped.pop("pending", None)
    document = {"item": dumped, "workflow": workflow, "prices": used}
    return hashlib.sha256(json.dumps(document, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class _Planned:
    item: DemoWorkItem
    state: str
    history: list[tuple[str, datetime, str | None, str | None]]
    costs: dict[str, float]
    digest: str
    existing: WorkItem | None = None


@dataclass
class _Plan:
    workflow: Workflow
    workflow_name: str
    workflow_version: int | None
    items: list[_Planned] = field(default_factory=list)


def _counts(item: DemoWorkItem) -> dict[str, int]:
    return {
        "work_items": 1,
        "runs": len(item.runs),
        "decisions": len(item.decisions),
        "findings": len(item.findings),
        "pending": 1 if item.pending is not None else 0,
    }


async def _members(session: AsyncSession, org_id: str, emails: set[str]) -> set[str]:
    if not emails:
        return set()
    rows = await session.execute(
        select(User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, User.email.in_(emails))
    )
    return {str(email) for (email,) in rows}


async def _agents(session: AsyncSession, org_id: str, slugs: set[str]) -> set[str]:
    if not slugs:
        return set()
    rows = await session.execute(select(Agent.slug).where(Agent.org_id == org_id, Agent.slug.in_(slugs)))
    return {str(slug) for (slug,) in rows}


def _refuse_duplicates(document: DemoHistory) -> None:
    seen: set[str] = set()
    for item in document.work_items:
        if item.id in seen:
            raise unprocessable(f"the work item id `{item.id}` is used twice in the document")
        seen.add(item.id)
        for kind, ids in (
            ("run", [r.id for r in item.runs]),
            # A waiting request is a decision not taken yet: one id space with the decisions.
            ("decision", [d.id for d in item.decisions] + ([item.pending.id] if item.pending else [])),
            ("finding", [f.id for f in item.findings]),
        ):
            for found in {i for i in ids if ids.count(i) > 1}:
                raise unprocessable(f"the {kind} id `{found}` is used twice in work item `{item.id}`")


def _states_of(item: DemoWorkItem, engine: WorkflowEngine, states: set[str]) -> tuple[str, list[Any]]:
    """The item's current state, after checking every state it names against the workflow."""
    named = [(entry.state, f"history of `{item.id}`") for entry in item.history]
    if item.state is not None:
        named.append((item.state, f"`state` of `{item.id}`"))
    for state, where in named:
        if state not in states:
            raise unprocessable(
                f"the state `{state}` ({where}) is not a state of the workflow; it declares: "
                + ", ".join(sorted(states))
            )
    moments = [_utc(entry.at) for entry in item.history]
    if moments != sorted(moments):
        raise unprocessable(f"the history of `{item.id}` is not in chronological order")
    if moments and moments[0] < _utc(item.created_at):
        raise unprocessable(f"the history of `{item.id}` starts before the work item was created")
    last = item.history[-1].state if item.history else None
    if item.state is not None and last is not None and item.state != last:
        raise unprocessable(
            f"`state` of `{item.id}` is `{item.state}` and its history ends in `{last}`: they must agree"
        )
    return (item.state or last or engine.initial_state), list(item.history)


def _check_pending(item: DemoWorkItem, engine: WorkflowEngine, state: str) -> None:
    """A waiting request on a work item that can still wait: not ended, and not asked before it existed."""
    if item.pending is None:
        return
    if engine.is_terminal(state):
        raise unprocessable(
            f"`pending` of `{item.id}`: the work item is in `{state}`, a terminal state, and an ended "
            "work item waits for no one"
        )
    if _utc(item.pending.requested_at) < _utc(item.created_at):
        raise unprocessable(f"`pending` of `{item.id}` is asked before the work item was created")


async def _plan(session: AsyncSession, project: Project, document: DemoHistory) -> _Plan:
    """Everything that can be refused, refused before the first row is written."""
    _refuse_duplicates(document)
    probe = WorkItemBirth(tracker_key="-", title="-", workflow=document.workflow)
    row = await choose_workflow(session, project, probe)
    workflow = workflow_model(row)
    engine = WorkflowEngine(workflow)
    states = set(workflow.states)
    plan = _Plan(
        workflow=workflow,
        workflow_name=row.name if row is not None else workflow.metadata.name,
        workflow_version=row.version if row is not None else workflow.metadata.version,
    )

    people: set[str] = set()
    agents: set[str] = set()
    for item in document.work_items:
        people |= {p for p in [item.created_by, *(d.by for d in item.decisions)] if p}
        for entry in item.history:
            if entry.by:
                (people if "@" in entry.by else agents).add(entry.by)
        agents |= {r.agent for r in item.runs if r.agent}
    known_people = await _members(session, project.org_id, people)
    known_agents = await _agents(session, project.org_id, agents)
    for person in sorted(people - known_people):
        raise unprocessable(f"`{person}` is not a member of the organisation: add them first")
    for slug in sorted(agents - known_agents):
        raise unprocessable(f"the agent `{slug}` is not in the organisation's registry: create it first")

    for item in document.work_items:
        state, history = _states_of(item, engine, states)
        _check_pending(item, engine, state)
        for finding in item.findings:
            if finding.run is not None and finding.run not in {r.id for r in item.runs}:
                raise unprocessable(
                    f"finding `{finding.id}` names the run `{finding.run}`, not in `{item.id}`"
                )
            # The type and its severity are the workflow's to judge (ADR 0217), as at the finding routes.
            refused = finding_type_refusal(workflow, finding.type, finding.severity)
            if refused is not None:
                raise unprocessable(f"finding `{finding.id}` of `{item.id}`: {refused}")
        # The ticket's fields are the workflow's to judge, as for a live ticket; nothing is written yet.
        await verify_birth(
            session,
            project,
            WorkItemBirth(
                tracker_key=f"{KEY_PREFIX}{item.id}",
                title=item.title,
                size=item.size,
                workflow=document.workflow,
                fields=dict(item.fields),
            ),
        )
        costs = {run.id: cost_of(run, document.prices) for run in item.runs}
        existing = (
            await session.execute(
                select(WorkItem).where(
                    WorkItem.id == derived_id(project.id, "item", item.id),
                )
            )
        ).scalar_one_or_none()
        planned = _Planned(
            item=item,
            state=state,
            history=[(h.state, _utc(h.at), h.by, h.reason) for h in history],
            costs=costs,
            digest=digest_of(item, plan.workflow_name, document.prices),
            existing=existing,
        )
        if existing is not None:
            held = ((existing.documents or {}).get(MARKER) or {}).get("digest")
            if not existing.imported or held != planned.digest:
                raise conflict(
                    f"work item `{item.id}` was already imported with other content (or the key "
                    f"`{KEY_PREFIX}{item.id}` is taken): re-import the same document, or give it a new `id`"
                )
        plan.items.append(planned)
    return plan


def _result_of(run: DemoRun) -> dict[str, Any]:
    outcome = "done" if run.status == "succeeded" else "failed"
    summary = run.summary or f"{run.stage} {run.status.replace('_', ' ')}"
    result: dict[str, Any] = {"schema": "choregos/StageResult/v1", "status": outcome, "summary": summary}
    if outcome == "failed":
        result["reason"] = run.status
    return result


async def _write_runs(
    session: AsyncSession, project: Project, plan: _Plan, planned: _Planned, item: WorkItem, size: str | None
) -> tuple[float, dict[str, Any]]:
    fx = get_settings().fx_usd_eur
    totals = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": 0.0, "duration_s": 0.0}
    ledger: list[CostLedger] = []
    for run in planned.item.runs:
        started = _utc(run.started_at)
        ended = started + timedelta(seconds=run.duration_s or 0.0)
        cost = planned.costs[run.id]
        tokens = run.tokens.model_dump() if run.tokens else {"input": 0, "output": 0, "cached": 0}
        run_id = derived_id(project.id, "run", planned.item.id, run.id)
        row = Run(
            id=run_id,
            work_item_id=item.id,
            project_id=project.id,
            stage_role=run.stage,
            attempt=run.attempt,
            actor=run.agent or run.stage,
            model=run.model,
            agent_slug=run.agent,
            status=run.status,
            started_at=started,
            ended_at=ended,
            decided_at=ended,
            tokens={
                "tokens_in": tokens["input"],
                "tokens_out": tokens["output"],
                "tokens_cached": tokens["cached"],
                "cost_usd": cost,
                "cost_eur": round(cost * fx, 6),
                "duration_s": run.duration_s or 0.0,
                "runs": 1,
            },
            cost_usd=cost,
            spend_collected=True,
            result=_result_of(run),
            imported=True,
        )
        row.created_at = started
        session.add(row)
        ledger.append(
            CostLedger(
                id=derived_id(project.id, "cost", planned.item.id, run.id),
                project_id=project.id,
                work_item_id=item.id,
                run_id=run_id,
                kind="model",
                provider=(run.model or "").split("/", 1)[0] if "/" in (run.model or "") else "",
                model=run.model or "",
                stage_role=run.stage,
                size=size,
                tokens_in=tokens["input"],
                tokens_out=tokens["output"],
                tokens_cached=tokens["cached"],
                cost_usd=cost,
                cost_eur=round(cost * fx, 6),
                fx_rate=fx,
                ts=ended,
                agent_slug=run.agent,
                workflow_name=plan.workflow_name,
                workflow_version=plan.workflow_version,
                imported=True,
            )
        )
        totals["tokens_in"] += tokens["input"]
        totals["tokens_out"] += tokens["output"]
        totals["tokens_cached"] += tokens["cached"]
        totals["cost_usd"] += cost
        totals["duration_s"] += run.duration_s or 0.0
    # The runs first: a ledger line's foreign key must find its run (the ledger maps no relationship).
    await session.flush()
    session.add_all(ledger)
    cost_usd = round(totals["cost_usd"], 6)
    return cost_usd, {
        **totals,
        "cost_usd": cost_usd,
        "cost_eur": round(cost_usd * fx, 6),
        "runs": len(planned.item.runs),
    }


def _write_decisions(session: AsyncSession, project: Project, planned: _Planned, item: WorkItem) -> None:
    for decision in planned.item.decisions:
        decided = _utc(decision.at)
        asked = _utc(decision.requested_at) if decision.requested_at else decided - timedelta(hours=1)
        kind = HumanRequestKind(decision.kind)
        row = new_human_request(
            work_item_id=item.id,
            project_id=project.id,
            transition_id=None,
            kind=decision.kind,
            payload={"summary": decision.summary, "question": decision.summary, "imported": True},
        )
        row.id = derived_id(project.id, "decision", planned.item.id, decision.id)
        row.requested_at = asked
        row.due_at = asked + timedelta(hours=24)
        row.created_at = asked
        row.decided_by = decision.by
        row.decided_at = decided
        row.decision = HumanDecision(
            request_id=row.id,
            kind=kind,
            approved=None if kind is HumanRequestKind.QUESTION else decision.approved,
            answer=decision.answer,
            reason=decision.reason,
            decided_by=decision.by,
            decided_at=decided,
            channel="api",
        ).model_dump(mode="json")
        session.add(row)


def _write_pending(session: AsyncSession, project: Project, planned: _Planned, item: WorkItem) -> None:
    """The request that still waits: the row a live step writes, so the inbox lists it as any other.

    No transition: no workflow runs the work item, so no actor is named and anyone who may decide in the
    project may answer it. The payload says it was imported; the work item's own flag is what the decision
    route reads (a payload is the workflow's to write, a column is not)."""
    pending = planned.item.pending
    if pending is None:
        return
    asked = _utc(pending.requested_at)
    row = new_human_request(
        work_item_id=item.id,
        project_id=project.id,
        transition_id=None,
        kind=pending.kind,
        payload={"summary": pending.summary, "question": pending.summary, "imported": True},
    )
    row.id = derived_id(project.id, "decision", planned.item.id, pending.id)
    row.requested_at = asked
    row.due_at = _utc(pending.due_at) if pending.due_at else asked + timedelta(hours=24)
    row.created_at = asked
    session.add(row)


def _write_findings(session: AsyncSession, project: Project, planned: _Planned, item: WorkItem) -> None:
    for finding in planned.item.findings:
        row = Finding(
            id=derived_id(project.id, "finding", planned.item.id, finding.id),
            project_id=project.id,
            origin_work_item_id=item.id,
            origin_run_id=(
                derived_id(project.id, "run", planned.item.id, finding.run) if finding.run else None
            ),
            title=finding.title,
            type=str(finding.type),
            severity=finding.severity,
            evidence=finding.evidence,
            suggested_fix=finding.suggested_fix,
            estimate=finding.estimate,
            status=finding.status,
            imported=True,
        )
        row.created_at = _utc(finding.at or planned.item.created_at)
        session.add(row)


async def _write_item(
    session: AsyncSession,
    principal: Principal,
    project: Project,
    document: DemoHistory,
    plan: _Plan,
    planned: _Planned,
) -> None:
    source = planned.item
    item = await new_work_item(
        session,
        project,
        WorkItemBirth(
            tracker_key=f"{KEY_PREFIX}{source.id}",
            title=source.title,
            body=source.body,
            url=source.url,
            size=source.size,
            risk=source.risk,
            created_by=source.created_by,
            reporter=source.created_by,
            initiated_by=initiator("demo", principal.email),
            workflow=document.workflow,
            fields=dict(source.fields),
            item_id=derived_id(project.id, "item", source.id),
        ),
    )
    validate_fields(plan.workflow.metadata.inputs, dict(item.fields), plan.workflow.metadata.name)
    engine = WorkflowEngine(plan.workflow)
    created = _utc(source.created_at)
    item.created_at = created
    item.state = planned.state
    item.imported = True
    last = planned.history[-1][1] if planned.history else created
    item.updated_at = last
    item.closed_at = last if engine.is_terminal(planned.state) else None
    # The state history: one `state_changed` event per move, at its own instant, like the interpreter's.
    previous = engine.initial_state
    for state, at, by, reason in planned.history:
        if state == previous:
            continue
        session.add(
            Event(
                project_id=project.id,
                work_item_id=item.id,
                type=str(EventType.WORKITEM_STATE_CHANGED),
                subject=item.tracker_key,
                payload={
                    "from": previous,
                    "to": state,
                    "by": by,
                    "reason": reason,
                    "imported": True,
                },
                ts=at,
            )
        )
        previous = state
    _, totals = await _write_runs(session, project, plan, planned, item, source.size)
    item.totals = totals
    item.documents = {**(item.documents or {}), MARKER: {"id": source.id, "digest": planned.digest}}
    _write_decisions(session, project, planned, item)
    _write_pending(session, project, planned, item)
    _write_findings(session, project, planned, item)
    await session.flush()


async def import_history(
    session: AsyncSession, principal: Principal, project: Project, document: DemoHistory
) -> DemoHistoryImport:
    """Checks the whole document, then writes what the project does not hold yet; audits the import."""
    plan = await _plan(session, project, document)
    created = {"work_items": 0, "runs": 0, "decisions": 0, "findings": 0, "pending": 0}
    unchanged = dict(created)
    cost_usd = 0.0
    for planned in plan.items:
        counts = _counts(planned.item)
        if planned.existing is not None:
            for name, number in counts.items():
                unchanged[name] += number
            continue
        await _write_item(session, principal, project, document, plan, planned)
        for name, number in counts.items():
            created[name] += number
        cost_usd += sum(planned.costs.values())
    await record(
        session,
        principal,
        "demo_history.import",
        org_id=project.org_id,
        target_type="project",
        target_id=project.id,
        created=created,
        unchanged=unchanged,
        cost_usd=round(cost_usd, 6),
    )
    return DemoHistoryImport(
        created=DemoHistoryCounts(**created),
        unchanged=DemoHistoryCounts(**unchanged),
        cost_usd=round(cost_usd, 6),
    )


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


async def remove_history(session: AsyncSession, principal: Principal, project: Project) -> DemoHistoryRemoval:
    """Deletes what imports wrote into the project: the rows marked `imported`, and nothing else; audited.

    The findings and the cost lines go first, by their own mark (their key to a work item is `SET NULL`, so a
    work item's deletion would leave them behind, unattached); then the imported work items, whose runs,
    requests and events go with them (`CASCADE`). A row that is not marked is never touched, whatever it
    points at.
    """
    items = select(WorkItem.id).where(WorkItem.project_id == project.id, WorkItem.imported.is_(True))
    cost_where = (CostLedger.project_id == project.id, CostLedger.imported.is_(True))
    cost_usd = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(CostLedger.cost_usd), 0.0)).where(*cost_where)
            )
        ).scalar_one()
    )
    removed = DemoHistoryRemoval(
        work_items=await _count(session, WorkItem, WorkItem.id.in_(items)),
        runs=await _count(session, Run, Run.project_id == project.id, Run.imported.is_(True)),
        requests=await _count(session, HumanRequest, HumanRequest.work_item_id.in_(items)),
        findings=await _count(session, Finding, Finding.project_id == project.id, Finding.imported.is_(True)),
        cost_lines=await _count(session, CostLedger, *cost_where),
        cost_usd=round(cost_usd, 6),
    )
    await session.execute(delete(CostLedger).where(*cost_where))
    await session.execute(delete(Finding).where(Finding.project_id == project.id, Finding.imported.is_(True)))
    await session.execute(delete(Run).where(Run.project_id == project.id, Run.imported.is_(True)))
    await session.execute(delete(HumanRequest).where(HumanRequest.work_item_id.in_(items)))
    await session.execute(delete(Event).where(Event.work_item_id.in_(items)))
    await session.execute(
        delete(WorkItem).where(WorkItem.project_id == project.id, WorkItem.imported.is_(True))
    )
    await record(
        session,
        principal,
        "demo_history.delete",
        org_id=project.org_id,
        target_type="project",
        target_id=project.id,
        **removed.model_dump(),
    )
    return removed
