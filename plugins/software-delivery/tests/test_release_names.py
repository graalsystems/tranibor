"""The release train's event types and statuses are the plugin's (ADR 0181, round D).

The contracts' internal event types are an open list since this round (`EVENT_TYPES`, the schema a pattern):
the extension declares `choregos.release.*`, under the strings subscribers and the `events` rows key on, and
holds its own `ReleaseStatus`. The core kept the eight types, `EventType.RELEASE_*`, `ReleaseStatus` and two
unused domain models for the window `D-train`, which closed early on 2026-11-23 (S83-04r): they are gone.
"""

from __future__ import annotations

import pytest
import tranibor_contracts
import tranibor_core
from pydantic import ValidationError
from tranibor_contracts import EVENT_TYPES, ChoregosEvent, EventType
from tranibor_contracts.open_lists import STORED
from tranibor_software_delivery import NAME
from tranibor_software_delivery.lists import RELEASE_EVENT_TYPES
from tranibor_software_delivery.names import ReleaseEvent, ReleaseStatus


def test_the_extension_declares_the_train_s_event_types_under_their_strings() -> None:
    assert EVENT_TYPES.declared_by(NAME) == {member.value for member in ReleaseEvent} == RELEASE_EVENT_TYPES
    for member in ReleaseEvent:
        assert EVENT_TYPES.plugin_of(member.value) == NAME
        assert ChoregosEvent.emit(member, project_slug="p").type == member.value


def test_the_plugin_holds_the_statuses_the_releases_rows_store() -> None:
    assert {s.value for s in ReleaseStatus} == {
        "collecting",
        "departing",
        "staging",
        "awaiting_approval",
        "promoting",
        "verifying",
        "done",
        "rolled_back",
        "frozen",
    }


def test_the_core_keeps_none_of_them() -> None:
    assert not {m.value for m in EventType} & RELEASE_EVENT_TYPES
    assert not RELEASE_EVENT_TYPES & EVENT_TYPES.core
    assert not hasattr(tranibor_contracts, "ReleaseStatus")
    assert not hasattr(tranibor_core, "ReleaseRecord") and not hasattr(tranibor_core, "ReleaseItem")


def test_a_type_no_one_declares_is_refused_and_a_stored_one_is_read() -> None:
    with pytest.raises(ValidationError, match="unknown event type"):
        ChoregosEvent(type="choregos.incident.opened", source="/test")
    stored = ChoregosEvent.model_validate(
        {"type": "choregos.incident.opened", "source": "/test"}, context={STORED: True}
    )
    assert stored.type == "choregos.incident.opened"
