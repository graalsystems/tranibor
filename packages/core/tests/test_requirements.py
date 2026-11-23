"""What a project's workflows require of its connectors (ADR 0034, S19-01).

The product review: the settings of an HR project showed `scm`, `ci`, `cd`. A project only has to
connect what its workflows read; each requirement says why.

What a guarantee, a step effect or a carrier requires is what its extension declares when it registers it:
the core's mechanism runs here on a stand-in plugin's (`stand_ins`), the same with or without the plugins'
extensions. What the `software-delivery` plugin's own gates, effects and train require, and so its
delivery templates, is pinned in `plugins/software-delivery/tests/core/test_requirements.py` (S83-05n).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from tranibor_core import carriers, gates, parse_workflow, step_effects
from tranibor_core.dsl import template_yaml
from tranibor_core.dsl.requirements import requirements
from tranibor_core.gates import GateContext, GateOutcome, gate_needs, gate_plugin, known_gates
from tranibor_core.step_effects import PASSED, StepEffectRequest

STAFFING = Path(__file__).resolve().parents[3] / "demo/workflows/staffing.yaml"

ARRIVAL = """apiVersion: choregos/v1
kind: Workflow
metadata: { name: arrival, version: 1 }
actors:
  coordinator: { type: agent, role: plan, model: "profile:standard" }
  hr: { type: human, group: hr, sla_hours: 24 }
states:
  request: { display: Request, kind: wait }
  plan: { display: Access plan }
  validated: { display: Validated, terminal: true }
transitions:
  - { id: t-plan, from: request, to: plan, by: coordinator, outputs: [plan], gates: [outputs_present] }
  - { id: t-validate, from: plan, to: validated, by: hr }
"""


def _capabilities(*texts: str) -> list[str]:
    return [e.capability for e in requirements(parse_workflow(t)[0] for t in texts)]


#: A stand-in plugin: what its extension registers when it loads, and removes when the test ends.
STAND_IN = "stand-in"

#: A release, on the stand-in's gate (`ledger_checked`, reads the `scm`), its step effect
#: (`publish_notes`, through the `scm`) and the train that carries it to production (needs a `cd`).
RELEASE = """apiVersion: choregos/v1
kind: Workflow
metadata: { name: release, version: 1 }
actors:
  writer: { type: agent, role: plan, model: "profile:standard" }
  desk: { type: system }
  captain: { type: human, group: release-captains, sla_hours: 4 }
states:
  todo: { display: To do, kind: wait }
  written: { display: Written }
  published: { display: Published }
  live: { display: Live, production: true, terminal: true }
transitions:
  - { id: t-write, from: todo, to: written, by: writer, outputs: [notes], gates: [outputs_present] }
  - { id: t-publish, from: written, to: published, by: desk, does: publish_notes }
  - id: t-ship
    from: published
    to: live
    via: release_train
    train: { env: prod, approval: captain }
"""


async def _publish(request: StepEffectRequest) -> Mapping[str, Any]:
    return {}


@pytest.fixture
def stand_ins() -> Iterator[None]:
    """The stand-in's gate, step effect and carrier. The DSL knows one carrier, `release_train`: the
    stand-in declares it in place of whoever did (the `software-delivery` extension, when installed), which
    is put back after."""
    declared, needs = dict(carriers._CARRIERS), dict(carriers._NEEDS)

    @gates.gate("ledger_checked", needs=("scm",), plugin=STAND_IN)
    def _ledger_checked(ctx: GateContext, params: dict[str, Any]) -> GateOutcome:
        return GateOutcome("ledger_checked", True)

    step_effects.step_effect(
        "publish_notes",
        gesture="publishes the notes",
        when=PASSED,
        plugin=STAND_IN,
        execute=_publish,
        needs=("scm",),
    )
    carriers._CARRIERS.pop("release_train", None)
    carriers.declare_carrier("release_train", plugin=STAND_IN, needs=("cd",))
    try:
        yield
    finally:
        gates.forget(STAND_IN)
        step_effects.forget(STAND_IN)
        carriers._CARRIERS.clear()
        carriers._CARRIERS.update(declared)
        carriers._NEEDS.clear()
        carriers._NEEDS.update(needs)


def test_the_default_request_workflow_needs_no_repository_ci_or_cd() -> None:
    """ADR 0110 §2: a step that works in a repository, a production state or a pull request turns it red."""
    assert _capabilities(template_yaml("default-request")) == ["tracker", "runtime", "gateway"]


def test_an_hr_process_requires_neither_a_repository_nor_a_ci_nor_a_train() -> None:
    assert _capabilities(ARRIVAL) == ["tracker", "runtime", "gateway"]
    assert _capabilities(STAFFING.read_text(encoding="utf-8")) == ["tracker", "runtime", "gateway"]


def test_each_requirement_says_why_and_where_from(stand_ins: None) -> None:
    """The reason names the workflow, the guarantee and its step; twice the same is said once."""
    text = ARRIVAL.replace("gates: [outputs_present]", "gates: [outputs_present, ledger_checked]")
    workflow = parse_workflow(text.replace("role: plan", "role: architect"))[0]
    (scm,) = [e for e in requirements([workflow, workflow]) if e.capability == "scm"]
    assert "arrival: the guarantee ledger_checked on t-plan" in scm.reasons
    assert any("coordinator (architect) works in a repository" in reason for reason in scm.reasons)
    assert len(set(scm.reasons)) == len(scm.reasons) == 2, "without duplicates"


def test_a_guarantee_requires_what_its_extension_declares_even_in_an_hr_process(stand_ins: None) -> None:
    with_ledger = ARRIVAL.replace("gates: [outputs_present]", "gates: [outputs_present, ledger_checked]")
    assert gate_needs("ledger_checked") == {"scm"} and gate_plugin("ledger_checked") == STAND_IN
    assert "scm" in _capabilities(with_ledger) and "ci" not in _capabilities(with_ledger)


def test_each_core_guarantee_knows_what_it_reads() -> None:
    """A guarantee that reads a diff or a CI without declaring it would leave a project without the
    connector it needs, and the guarantee would fail on the first run. The core's own read none: they
    judge the stage result (a plugin's say what they read when they register)."""
    reads = {g: gate_needs(g) for g in known_gates()}
    assert reads["outputs_present"] == frozenset()
    assert {g: r for g, r in reads.items() if gate_plugin(g) is None and r} == {}


def test_what_the_platform_does_is_required_by_the_effect_not_by_the_name(stand_ins: None) -> None:
    """#175: a renamed state keeps what it requires, because the effect is written (`does`, `production`)."""
    from tranibor_core.dsl.edition import edit

    text = edit(
        RELEASE,
        [
            {"op": "rename_state", "from": "published", "to": "announced"},
            {"op": "rename_state", "from": "live", "to": "in_service"},
        ],
    ).yaml
    workflow, report = parse_workflow(text, strict=False)
    assert report.valid, [i.format() for i in report.errors]
    reasons = {e.capability: " ".join(e.reasons) for e in requirements([workflow])}
    assert "t-publish publishes the notes" in reasons["scm"]
    assert "in_service is production" in reasons["cd"]


def test_the_architect_of_a_study_writes_in_the_repository() -> None:
    """It writes `docs/adr/NNNN-*.md` on a branch (S21-22): its project connects a repository."""
    study = ARRIVAL.replace("role: plan", "role: architect")
    (scm,) = [e for e in requirements([parse_workflow(study)[0]]) if e.capability == "scm"]
    assert any("coordinator (architect) works in a repository" in r for r in scm.reasons)


def test_a_documentation_step_works_in_the_repository() -> None:
    """The `document` role reads a diff and pushes a branch: it requires `scm` (S39-15)."""
    text = ARRIVAL.replace("role: plan", "role: document")
    assert "scm" in _capabilities(text)


def test_a_decision_step_needs_the_gateway_and_no_runtime_of_its_own() -> None:
    """ADR 0121: a decision runs in the orchestrator and asks its model through the gateway."""
    needs = _capabilities(template_yaml("decision-triage"))
    assert needs == ["tracker", "runtime", "gateway"], "the `deliver` agent still runs somewhere"
    reasons = {
        e.capability: e.reasons for e in requirements([parse_workflow(template_yaml("decision-triage"))[0]])
    }
    assert any("decision classifier" in r for r in reasons["gateway"])
    assert not any("classifier" in r for r in reasons["runtime"])
