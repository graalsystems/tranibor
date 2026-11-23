# SPDX-License-Identifier: Apache-2.0
"""Gate evaluation: the mechanism that verifies what the agent claims."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from temporalio import activity
from tranibor_api.db.models import CostLedger, Run
from tranibor_contracts import StageResult
from tranibor_contracts.open_lists import STORED
from tranibor_core import GateContext, GateOutcome, evaluate
from tranibor_core.gates import ProviderRequest, after_judgment, provide_facts
from tranibor_core.run_journal import GATE_OUTCOME

from .base import db, load_work_item, project_bundle


@activity.defn(name="evaluate_gates")
async def evaluate_gates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate the gates of a transition against what the step produced and the external state.

    The synchronous gates decide at once; the asynchronous ones return `pending` until the awaited
    event (an action's outcome, a flag, a pull request's checks) has arrived. What a plugin's gates read
    — the diff and the pull request of software delivery — is its context provider's to read (ADR 0181,
    round C2): this activity calls each provider a declared gate reads, then each provider's `judged`
    hook once the verdicts are in.
    """
    gates: list[dict[str, Any]] = payload.get("gates", [])
    if not gates:
        return []
    async with db() as session:
        bundle = await project_bundle(session, payload["project_id"])
        item = await load_work_item(session, payload["work_item_id"])
        run = await session.get(Run, payload["run_id"]) if payload.get("run_id") else None
        result = (
            StageResult.model_validate(run.result, context={STORED: True}) if run and run.result else None
        )

        # What the run really called in the catalogue: the registry, not the result. A place held under
        # the tool-call cap is not a call (S57-03): one whose request died never answered.
        calls: list[str] = []
        if run is not None:
            from sqlalchemy import select
            from tranibor_core.tool_ledger import IN_FLIGHT

            rows = await session.execute(
                select(CostLedger.model).where(
                    CostLedger.run_id == run.id, CostLedger.kind == "tool", CostLedger.provider != IN_FLIGHT
                )
            )
            calls = [str(m) for m in rows.scalars() if m]
        request = ProviderRequest(
            project_id=str(payload["project_id"]),
            work_item_id=str(payload["work_item_id"]),
            run_id=str(run.id) if run is not None else None,
            result=result,
            fields=dict(item.fields or {}),
            documents=dict(item.documents or {}),
            project=bundle,
            session=session,
        )
        # What the extensions' context providers read for the gates that declare them (ADR 0181).
        facts = await provide_facts(gates, request)
        context = GateContext(
            result=result,
            tool_calls=calls,
            flags=list(payload.get("flags", [])),
            expected_outputs=list(payload.get("expected_outputs", [])),
            required_flag=payload.get("required_flag"),
            documents=dict(item.documents or {}),
            fields=dict(item.fields or {}),
            parent_documents=await _parent_documents(session, item),
            facts=facts,
        )
        outcomes = [evaluate(gate["name"], context, dict(gate.get("params", {}))) for gate in gates]

        # What the plugins do with the verdicts (a pull request's checks, the commit a merge names),
        # and what they add to each verdict's journal line (`head_sha`).
        declared = tuple((str(g["name"]), dict(g.get("params") or {})) for g in gates)
        noted = await after_judgment(replace(request, gates=declared), facts, outcomes)
        if run is not None:
            await _journal(session, run, outcomes, noted.get("head_sha"))
        return [
            {
                "name": o.name,
                "passed": o.passed,
                "pending": o.pending,
                "detail": o.detail,
                "annotations": list(o.annotations),
            }
            for o in outcomes
        ]


async def _parent_documents(session: Any, item: Any) -> dict[str, Any] | None:
    """The documents of the work item that opened this one by a fan-out, read from the parent's own
    row; `None` for a work item nobody spawned. `documents.parent` is written by the platform
    only (the API never accepts documents from a person), so a hand-made child has none (S38-17)."""
    from .children import PARENT

    link = (item.documents or {}).get(PARENT)
    if not isinstance(link, dict) or not link.get("work_item_id"):
        return None
    parent = await load_work_item(session, link["work_item_id"])
    if parent.project_id != item.project_id:
        return None
    return dict(parent.documents or {})


async def _journal(session: Any, run: Run, outcomes: list[GateOutcome], head: str | None = None) -> None:
    """File each verdict in the run's journal (`gate.outcome`), with the commit it was given on
    (`head_sha`, ADR 0171) when the evaluation judged one.

    The verdicts were only returned to the workflow, which decided and then forgot them: no screen
    could say WHICH guarantees had run nor what they had answered, and the product's central promise
    was invisible (state of play of 2026-09-24). The run's journal is what the page already shows;
    the verdicts take their place there.

    Numbered after the highest line, at 1 at least (S65-28, finding #1608). The verdicts are judged
    once the run's process has finished, and the runner posts its last line (`run.result`) before it
    posts its result: the numbers after the highest are no longer the runner's to take. A journal that
    holds only the platform's lines from before the run (`memory.skipped`, numbered below 0) does not
    pull a verdict down to 0, a number a run may still send.
    """
    from sqlalchemy import func, select
    from tranibor_api.db.models import RunEvent
    from tranibor_core import utcnow

    last = (await session.execute(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id))).scalar()
    seq = max(int(last or 0), 0)
    for outcome in outcomes:
        seq += 1
        session.add(
            RunEvent(
                run_id=run.id,
                seq=seq,
                type=GATE_OUTCOME,
                payload={
                    "name": outcome.name,
                    "passed": outcome.passed,
                    "pending": outcome.pending,
                    "detail": outcome.detail,
                    "annotations": list(outcome.annotations),
                    **({"head_sha": head} if head else {}),
                },
                ts=utcnow(),
            )
        )
