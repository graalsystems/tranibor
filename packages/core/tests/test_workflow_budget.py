"""The workflow's `budget` block (S27-06, ADR 0047), on `default-simple`'s front half on the core's gates
(`CORE_SIMPLE`): the block is the core's, whatever plugin a workflow names (S83-05n)."""

from __future__ import annotations

from tranibor_contracts import WorkflowBudget
from tranibor_core import WorkflowEngine, parse_workflow
from tranibor_core.dsl.validator import validate_workflow
from tranibor_testkit.workflows import CORE_SIMPLE


def _with_budget(on_exceeded: str) -> WorkflowEngine:
    wf = parse_workflow(CORE_SIMPLE)[0].model_copy(
        deep=True, update={"budget": WorkflowBudget(ticket_usd=3, on_exceeded=on_exceeded)}
    )
    return WorkflowEngine(wf)


def test_on_exceeded_comes_before_the_defaults() -> None:
    engine = _with_budget("abandoned")
    assert engine.after_budget_exceeded(engine.select_transition("ready")).next_state == "abandoned"


def test_on_exceeded_must_name_a_state() -> None:
    report = validate_workflow(_with_budget("nowhere").wf)
    assert any("budget.on_exceeded" in issue.message for issue in report.errors)
    assert validate_workflow(_with_budget("abandoned").wf).valid


def test_the_block_round_trips_through_the_schema() -> None:
    source = CORE_SIMPLE + "budget: {ticket_usd: 2.5, on_exceeded: needs_human}\n"
    wf, report = parse_workflow(source, strict=False)
    assert report.valid, report.errors
    assert wf.budget == WorkflowBudget(ticket_usd=2.5, on_exceeded="needs_human")
