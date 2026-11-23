"""The "process" view: each transition said in plain words (ADR 0031)."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from tranibor_core import parse_workflow, to_process
from tranibor_core.dsl.process import gate_summary
from tranibor_core.gates import GateContext, GateOutcome, forget, gate, known_gates

ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture
def waiting_gate() -> Iterator[None]:
    """A stand-in plugin's asynchronous gate, registered as its extension would, removed after."""

    def _detail(params: Mapping[str, Any]) -> str:
        if "approvals" in params:
            return f"at least {params['approvals']} approvals"
        return "at least 2 approvals (the platform's defaults)"

    @gate("approvals_counted", asynchronous=True, plugin="stand-in", detail=_detail)
    def _counted(ctx: GateContext, params: dict[str, Any]) -> GateOutcome:
        return GateOutcome("approvals_counted", True)

    yield
    forget("stand-in")


def _staffing() -> list[dict[str, object]]:
    workflow, _ = parse_workflow((ROOT / "demo/workflows/staffing.yaml").read_text(encoding="utf-8"))
    return to_process(workflow)


def test_every_known_guarantee_has_a_summary() -> None:
    """A new guarantee without a summary would make the process view speak into the void: the core's
    have theirs in `RESUMES`, an extension's say it when they are registered (`gate(..., summary=)`)."""
    assert sorted(name for name in known_gates() if gate_summary(name, {}) == name) == []


def test_an_agent_its_outputs_its_guarantees_and_its_retries() -> None:
    sourcing = next(e for e in _staffing() if e["to"] == "sourcing")
    assert sourcing["actor_type"] == "agent"
    phrase = str(sourcing["sentence"])
    assert "the agent `sourceur`" in phrase and "it produced `profils`" in phrase
    assert "the declared outputs are present" in phrase
    assert "retries up to 2 time(s)" in phrase and "“Needs a human”" in phrase


def test_a_person_their_delay_and_the_rejection() -> None:
    validation = next(e for e in _staffing() if e["from"] == "a_valider")
    assert validation["actor_type"] == "human"
    phrase = str(validation["sentence"])
    assert "a person of group `staffing-managers`, within 48 h" in phrase
    assert "If rejected, back to “Sourcing”" in phrase and "times out after 72 h" in phrase


def test_a_guarantee_says_its_thresholds_and_how_long_it_waits(waiting_gate: None) -> None:
    """The map listed guarantees as bare names: what the limit is, and that the step waits, were not said
    anywhere. A gate says its thresholds when its extension registers it (`detail`, with its own defaults);
    an asynchronous one waits up to the transition's timeout, 24 h without one. The `software-delivery`
    plugin's (`diff_size_max`, `ci_green`...) are pinned in its suite (`tests/core/test_process_view.py`)."""
    from tranibor_core.dsl.process import gate_detail

    assert (
        gate_detail("approvals_counted", {})
        == "at least 2 approvals (the platform's defaults); waits for it up to 24 h"
    )
    assert (
        gate_detail("approvals_counted", {"approvals": 3}, 72)
        == "at least 3 approvals; waits for it up to 72 h"
    )
    assert gate_detail("outputs_present", {}) is None
    assert gate_detail("outputs_in", {"values": {"verdict": ["approve"]}}) == "`verdict` in `approve`"


def test_each_step_s_guarantees_carry_their_detail() -> None:
    for step in _staffing():
        for guarantee in step["gates"]:  # type: ignore[attr-defined]
            assert set(guarantee) == {"name", "summary", "detail", "asynchronous"}
