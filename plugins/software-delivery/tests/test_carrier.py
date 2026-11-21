"""`via: release_train` is carried by the plugin's train, through the carrier seam (ADR 0181, round D).

The interpreter boards a train by calling the activity `signal_train` by name (`carriers.BOARD_ACTIVITY`):
the plugin's extension registers it on the workers with the rest of the train, and declares the carrier.
Without the extension a workflow naming `via: release_train` is refused at publication, naming the plugin;
a reader that is not the deployment leaves that refusal to the deployment.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tranibor_core import carriers, parse_workflow
from tranibor_core.dsl.validator import CARRIER_UNAVAILABLE, deployment_issues
from tranibor_software_delivery import NAME
from tranibor_software_delivery.extension import provide_worker_parts, register

ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR = ROOT / "apps" / "orchestrator" / "src" / "tranibor_orchestrator"
WORKFLOW = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: carried, version: 1}
actors:
  dev: {type: agent, role: implement}
states:
  todo: {display: To do, kind: wait}
  merged: {display: Merged}
  live: {display: Live, terminal: true}
transitions:
  - {id: t-do, from: todo, to: merged, by: dev}
  - {id: t-ship, from: merged, to: live, via: release_train, train: {env: staging}}
"""


@pytest.fixture
def without_the_extension() -> Iterator[None]:
    carriers.forget(NAME)
    try:
        yield
    finally:
        register()


def test_the_extension_carries_the_train_and_registers_its_boarding_activity() -> None:
    from tranibor_orchestrator.queues import activity_name

    assert carriers.known_carriers() == ["release_train"]
    assert carriers.unavailable_carrier("release_train") is None
    names = {activity_name(a) for a in provide_worker_parts().activities}
    assert carriers.BOARD_ACTIVITY["release_train"] in names


def test_the_core_names_the_boarding_activity_only() -> None:
    from tranibor_orchestrator.queues import activity_name
    from tranibor_orchestrator.workflows import WORKFLOW_ACTIVITIES

    assert "signal_train" not in {activity_name(a) for a in WORKFLOW_ACTIVITIES}
    assert '@activity.defn(name="signal_train")' not in (
        ORCHESTRATOR / "activities" / "interpretation.py"
    ).read_text("utf-8")
    assert "signal_release_train" not in (ORCHESTRATOR / "train_client.py").read_text("utf-8")


def test_with_the_extension_a_carried_transition_validates() -> None:
    _, report = parse_workflow(WORKFLOW, strict=False)
    assert CARRIER_UNAVAILABLE not in [e.code for e in report.errors]


def test_without_the_extension_it_is_refused_naming_the_plugin(without_the_extension: None) -> None:
    parsed, report = parse_workflow(WORKFLOW, strict=False)
    [refused] = [e for e in report.errors if e.code == CARRIER_UNAVAILABLE]
    assert f"`{NAME}`" in refused.message and refused.path == "transitions[1].via"
    # A reader that is not the deployment (the CLI offline) leaves it to the deployment.
    elsewhere, _others = deployment_issues(parsed, report)
    assert refused in elsewhere


def test_a_carrier_the_dsl_does_not_know_or_another_plugin_s_is_refused() -> None:
    with pytest.raises(ValueError, match="DSL knows"):
        carriers.declare_carrier("ferry", plugin="itsm")
    with pytest.raises(ValueError, match="already declared by the plugin"):
        carriers.declare_carrier("release_train", plugin="itsm")
