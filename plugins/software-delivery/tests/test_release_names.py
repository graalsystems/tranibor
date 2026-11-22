"""The release train's event types and statuses are the plugin's (ADR 0181, round D).

The contracts' internal event types are an open list since this round (`EVENT_TYPES`, the schema a pattern):
the extension declares `choregos.release.*`, under the strings subscribers and the `events` rows key on, and
holds its own `ReleaseStatus`. The core keeps the eight types, `EventType.RELEASE_*` and `ReleaseStatus` for
the window `D-train` (`tranibor_contracts.window`), removed in the first release cut on or after 2026-12-14.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tranibor_contracts import EVENT_TYPES, ChoregosEvent, EventType
from tranibor_contracts import ReleaseStatus as ContractsReleaseStatus
from tranibor_contracts.open_lists import STORED
from tranibor_contracts.window import D_TRAIN, RELEASE_EVENT_TYPES, WINDOW
from tranibor_software_delivery import NAME
from tranibor_software_delivery.names import ReleaseEvent, ReleaseStatus


def test_the_extension_declares_the_train_s_event_types_under_their_strings() -> None:
    assert EVENT_TYPES.declared_by(NAME) == {member.value for member in ReleaseEvent} == RELEASE_EVENT_TYPES
    for member in ReleaseEvent:
        assert EVENT_TYPES.plugin_of(member.value) == NAME
        assert ChoregosEvent.emit(member, project_slug="p").type == member.value


def test_the_plugin_s_statuses_are_the_contracts_values() -> None:
    assert {s.value for s in ReleaseStatus} == {s.value for s in ContractsReleaseStatus}


def test_the_core_keeps_them_for_the_window_only() -> None:
    kept = {entry.name for entry in WINDOW if entry.window is D_TRAIN}
    assert {f"EventType.{m.name}" for m in EventType if m.value in RELEASE_EVENT_TYPES} <= kept
    assert {"ReleaseStatus", "ReleaseRecord", "ReleaseItem"} <= kept
    assert set(EVENT_TYPES.kept) == RELEASE_EVENT_TYPES
    assert D_TRAIN.removed_on.isoformat() == "2026-12-14"


def test_a_type_no_one_declares_is_refused_and_a_stored_one_is_read() -> None:
    with pytest.raises(ValidationError, match="unknown event type"):
        ChoregosEvent(type="choregos.incident.opened", source="/test")
    stored = ChoregosEvent.model_validate(
        {"type": "choregos.incident.opened", "source": "/test"}, context={STORED: True}
    )
    assert stored.type == "choregos.incident.opened"
