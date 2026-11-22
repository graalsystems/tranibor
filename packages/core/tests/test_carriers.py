"""The carrier seam (ADR 0181, round D): the core carries no `via:` of its own, and knows the release
train's by name only, so that a workflow naming it is refused naming the plugin that carries it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tranibor_core import carriers


@pytest.fixture
def no_carrier() -> Iterator[None]:
    saved, needs = dict(carriers._CARRIERS), dict(carriers._NEEDS)
    carriers._CARRIERS.clear()
    carriers._NEEDS.clear()
    try:
        yield
    finally:
        carriers._CARRIERS.update(saved)
        carriers._NEEDS.update(needs)


def test_the_core_carries_nothing_itself(no_carrier: None) -> None:
    assert carriers.known_carriers() == []
    why = carriers.unavailable_carrier("release_train")
    assert why is not None and "`software-delivery`" in why
    assert "no installed extension" in (carriers.unavailable_carrier("ferry") or "")


def test_a_declared_carrier_is_available_and_forgotten_with_its_plugin(no_carrier: None) -> None:
    carriers.declare_carrier("release_train", plugin="itsm")
    carriers.declare_carrier("release_train", plugin="itsm")  # again: nothing changes
    assert carriers.unavailable_carrier("release_train") is None
    carriers.forget("itsm")
    assert carriers.unavailable_carrier("release_train") is not None


def test_the_boarding_activity_is_the_history_s_name() -> None:
    assert carriers.BOARD_ACTIVITY == {"release_train": "signal_train"}
    assert set(carriers.PLUGIN_CARRIERS) == set(carriers.BOARD_ACTIVITY)


CARRIED = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: carried, version: 1}
actors:
  owner: {type: human, group: ops, sla_hours: 8}
states:
  todo: {display: To do, kind: wait}
  approved: {display: Approved}
  live: {display: Live, production: true, terminal: true}
transitions:
  - {id: t-ok, from: todo, to: approved, by: owner}
  - {id: t-live, from: approved, to: live, via: release_train, train: {env: staging, approval: owner}}
"""


def test_a_carrier_s_needs_are_its_workflows_requirements(no_carrier: None) -> None:
    """S83-05k: the core infers no `cd` from a train or a production state; the carrier says what it
    needs when it is declared, and a production state needs what the carrier that reaches it needs."""
    from tranibor_core import parse_workflow
    from tranibor_core.dsl.requirements import requirements

    workflow, _ = parse_workflow(CARRIED, strict=False)
    assert "cd" not in {r.capability for r in requirements([workflow])}
    carriers.declare_carrier("release_train", plugin="itsm", needs=("cd",))
    [cd] = [r for r in requirements([workflow]) if r.capability == "cd"]
    assert cd.reasons == (
        "carried: the state live is production",
        "carried: t-live goes through a release train",
    )
    carriers.forget("itsm")
    assert carriers.carrier_needs("release_train") == ()


def test_the_boarding_payload_is_read_from_the_transition() -> None:
    from tranibor_core import parse_workflow

    workflow, _ = parse_workflow(CARRIED, strict=False)
    carried = next(t for t in workflow.transitions if t.id == "t-live")
    assert carriers.carrier_of(carried) == "release_train"
    assert carriers.boarding_env(carried) == "staging"
    assert carriers.boarding_approval(workflow, carried) == {"required": True, "group": "ops"}
    assert carriers.carrier_of(workflow.transitions[0]) is None


def test_production_is_reached_through_a_carrier_only() -> None:
    from tranibor_core import parse_workflow

    direct = CARRIED.replace("via: release_train, train: {env: staging, approval: owner}", "by: owner")
    _, report = parse_workflow(direct, strict=False)
    assert "prod.requires_train" in {e.code for e in report.errors}
    _, carried = parse_workflow(CARRIED, strict=False)
    assert "prod.requires_train" not in {e.code for e in carried.errors}
