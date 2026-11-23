"""The open lists of ADR 0181 (C0): every current value stays valid, an extension declares more, and
anything else is refused as before.

Five enumerations of the contracts became a pattern in their schema and an `OpenList` in Python: the
inbound event types, the finding types, the memory kinds, the connector kinds, the stage roles; `does:`
is the sixth (`STEP_EFFECTS`). Nothing is renamed: a stored event, finding or workflow reads as it did.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar

import pytest
import tranibor_contracts as contracts
from pydantic import ValidationError
from tranibor_contracts import (
    CONNECTOR_KINDS,
    EVENT_TYPES,
    FINDING_TYPES,
    INBOUND_EVENT_TYPES,
    MEMORY_KINDS,
    STAGE_ROLES,
    STEP_EFFECTS,
    ConnectorKind,
    EventType,
    Finding,
    FindingType,
    InboundEvent,
    InboundEventType,
    MemoryKind,
    OpenList,
    StageRole,
    Transition,
)

LISTS: dict[str, tuple[OpenList, type[StrEnum] | None]] = {
    "inbound event types": (INBOUND_EVENT_TYPES, InboundEventType),
    "event types": (EVENT_TYPES, EventType),
    "finding types": (FINDING_TYPES, FindingType),
    "memory kinds": (MEMORY_KINDS, MemoryKind),
    "connector kinds": (CONNECTOR_KINDS, ConnectorKind),
    "stage roles": (STAGE_ROLES, StageRole),
    "step effects": (STEP_EFFECTS, None),
}

#: schema file -> (the JSON pointer of the open field, its list).
SCHEMA_FIELDS = {
    "inbound-event.schema.json": (("properties", "type"), INBOUND_EVENT_TYPES),
    "event.schema.json": (("properties", "type"), EVENT_TYPES),
    "finding.schema.json": (("properties", "type"), FINDING_TYPES),
    "context-pack.schema.json": (("$defs", "memory", "properties", "kind"), MEMORY_KINDS),
    "workflow.schema.json": (("$defs", "transition", "properties", "does"), STEP_EFFECTS),
}


@pytest.fixture
def plugin() -> Iterator[str]:
    yield "itsm"
    for open_list, _ in LISTS.values():
        open_list.forget("itsm")
        open_list.forget("other")


@pytest.mark.parametrize("name", list(LISTS))
def test_the_core_list_is_the_enumeration_and_no_value_left(name: str) -> None:
    open_list, enum = LISTS[name]
    if enum is not None:
        assert open_list.core == {member.value for member in enum}
        for member in enum:
            assert open_list.check(member.value) is member, "a core value comes back as its member"
    else:
        assert open_list.core == {"open_pr", "merge_pr"}


@pytest.mark.parametrize("schema_name", list(SCHEMA_FIELDS))
def test_the_schema_holds_the_list_s_pattern_and_no_enum(schema_name: str) -> None:
    pointer, open_list = SCHEMA_FIELDS[schema_name]
    node = contracts.load_schema(schema_name)
    for key in pointer:
        node = node[key]
    assert "enum" not in node, f"{schema_name}: the list is open (ADR 0181)"
    assert node["pattern"] == open_list.pattern
    assert all(re.fullmatch(open_list.pattern, value) for value in open_list.core)


def test_every_current_value_still_validates_in_the_models() -> None:
    for event_type in InboundEventType:
        event = InboundEvent(type=event_type.value, source="test", delivery_id="d")
        assert event.type is event_type
        assert event.dedup_key == f"test:d:{event_type.value}"
    for finding_type in FindingType:
        found = Finding(title="a finding", type=finding_type.value, severity="low", evidence="x")
        assert found.type is finding_type
    for effect in ("open_pr", "merge_pr"):
        transition = Transition.model_validate(
            {"id": "t", "from": "a", "to": "b", "by": "ci", "does": effect}
        )
        assert transition.does == effect


def test_an_unknown_value_is_refused_naming_the_list() -> None:
    with pytest.raises(ValidationError, match=r"unknown inbound event type: 'itsm\.incident\.opened'"):
        InboundEvent(type="itsm.incident.opened", source="test", delivery_id="d")
    with pytest.raises(ValidationError, match="unknown finding type"):
        Finding(title="a finding", type="sla-breach", severity="low", evidence="x")
    with pytest.raises(ValidationError, match="unknown step effect"):
        Transition.model_validate({"id": "t", "from": "a", "to": "b", "by": "ci", "does": "close_incident"})


def test_a_declared_value_is_accepted_as_a_string_and_forgotten_with_its_plugin(plugin: str) -> None:
    INBOUND_EVENT_TYPES.declare("itsm.incident.opened", plugin=plugin)
    FINDING_TYPES.declare("sla-breach", plugin=plugin)
    STEP_EFFECTS.declare("close_incident", plugin=plugin)
    event = InboundEvent(type="itsm.incident.opened", source="servicenow", delivery_id="d")
    assert event.type == "itsm.incident.opened" and not isinstance(event.type, StrEnum)
    assert Finding(title="a finding", type="sla-breach", severity="low", evidence="x").type == "sla-breach"
    does = {"id": "t", "from": "a", "to": "b", "by": "ci", "does": "close_incident"}
    assert Transition.model_validate(does).does == "close_incident"
    assert INBOUND_EVENT_TYPES.plugin_of("itsm.incident.opened") == plugin
    assert "itsm.incident.opened" in INBOUND_EVENT_TYPES.known()

    INBOUND_EVENT_TYPES.forget(plugin)
    with pytest.raises(ValidationError):
        InboundEvent(type="itsm.incident.opened", source="servicenow", delivery_id="d")


def test_a_value_is_one_plugin_s_and_matches_the_pattern(plugin: str) -> None:
    MEMORY_KINDS.declare("runbook", plugin=plugin)
    MEMORY_KINDS.declare("runbook", plugin=plugin)  # again, for the same plugin: nothing changes
    with pytest.raises(ValueError, match="already declared by the plugin `itsm`"):
        MEMORY_KINDS.declare("runbook", plugin="other")
    with pytest.raises(ValueError, match="must match"):
        INBOUND_EVENT_TYPES.declare("Incident Opened", plugin=plugin)
    # A plugin may take over a value of the core: still valid, and still the core's member. Another plugin
    # cannot take it from it.
    INBOUND_EVENT_TYPES.declare("alert.fired", plugin=plugin)
    assert INBOUND_EVENT_TYPES.plugin_of("alert.fired") == plugin
    with pytest.raises(ValueError, match="already declared by the plugin `itsm`"):
        INBOUND_EVENT_TYPES.declare("alert.fired", plugin="other")
    assert (
        InboundEvent(type="alert.fired", source="alertmanager", delivery_id="d").type
        is InboundEventType.ALERT_FIRED
    )


def test_stage_role_has_the_roles_the_playbooks_ship() -> None:
    """`brief` and `deliver` (ADR 0110) were playbook roles the enumeration lacked (ADR 0181)."""
    assert {StageRole.BRIEF.value, StageRole.DELIVER.value} == {"brief", "deliver"}


def test_a_stored_document_reads_a_value_nobody_declares_any_more() -> None:
    """ADR 0098 §4, readers accept: a stored workflow naming a plugin's step effect keeps parsing once
    the plugin is gone (its interpreter's history replays); a new one naming it is refused."""
    from tranibor_contracts.open_lists import STORED

    does = {"id": "t", "from": "a", "to": "b", "by": "ci", "does": "close_incident"}
    assert Transition.model_validate(does, context={STORED: True}).does == "close_incident"
    with pytest.raises(ValidationError, match="unknown step effect"):
        Transition.model_validate(does)
    with pytest.raises(ValidationError, match="unknown step effect"):
        Transition.model_validate({**does, "does": "Close Incident"}, context={STORED: True})
    stored = InboundEvent.model_validate(
        {"type": "itsm.incident.opened", "source": "servicenow", "delivery_id": "d"}, context={STORED: True}
    )
    assert stored.type == "itsm.incident.opened"


#: Software delivery's values, which the core's lists held until ADR 0181 closed the windows C2, D-train and
#: D (2026-11-23): only the `software-delivery` plugin's extension declares them now, under these strings.
DELIVERY: dict[str, tuple[OpenList, type[StrEnum], frozenset[str]]] = {
    "inbound event types": (
        INBOUND_EVENT_TYPES,
        InboundEventType,
        frozenset(
            {
                "scm.pr.opened",
                "scm.pr.synchronized",
                "scm.pr.review_submitted",
                "scm.pr.merged",
                "scm.pr.closed",
                "scm.check.completed",
                "ci.run.started",
                "ci.run.succeeded",
                "ci.run.failed",
                "cd.app.synced",
                "cd.app.degraded",
                "cd.rollout.completed",
                "cd.rollout.aborted",
            }
        ),
    ),
    "finding types": (FINDING_TYPES, FindingType, frozenset({"bug", "perf", "tech-debt", "flaky-test"})),
    "memory kinds": (MEMORY_KINDS, MemoryKind, frozenset({"flaky_test", "hotspot"})),
    "connector kinds": (CONNECTOR_KINDS, ConnectorKind, frozenset({"scm", "ci", "cd"})),
    "event types": (
        EVENT_TYPES,
        EventType,
        frozenset(
            f"choregos.release.{name}"
            for name in (
                "collected",
                "departed",
                "staged",
                "approval_requested",
                "promoted",
                "verified",
                "rolled_back",
                "frozen",
            )
        ),
    ),
}


@pytest.mark.parametrize("name", list(DELIVERY))
def test_software_delivery_s_values_are_no_longer_the_core_s(name: str) -> None:
    """The windows C2, D-train and D closed (ADR 0181, 2026-11-23): the members left the enumerations and the
    values the core's lists. A stored document still reads each one, installed or not (ADR 0098 §4)."""
    open_list, enum, values = DELIVERY[name]
    assert not values & open_list.core
    assert not values & {member.value for member in enum}
    for value in values:
        assert open_list.check(value, _stored()) == value, "a stored value is read whatever is installed"
        assert open_list.plugin_of(value) in {None, "software-delivery"}


def test_the_release_train_s_statuses_left_the_contracts() -> None:
    assert not hasattr(contracts, "ReleaseStatus")
    assert not hasattr(contracts.enums, "ReleaseStatus")


def _stored() -> Any:
    return _Stored()


class _Stored:
    """A `ValidationInfo` as pydantic hands it to a validator reading a stored document."""

    context: ClassVar[dict[str, bool]] = {"stored": True}
