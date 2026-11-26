"""A state change writes its label under the new prefix and removes the previous one under either prefix
(ADR 0226, R9c).

Tracker labels are stored state at the tracker: an issue labelled before 0.30.0 carries `choregos:<state>`.
`mirror_state` names, beside the new label, every state label the issue may still carry for the previous
state and the former twin of the new one, so that no tracker (GitHub, GitLab's scoped labels, Jira, Azure
Boards, a plugin's) leaves two states on an issue. Replayed, it asks for the same thing again (ADR 0008).
"""

from __future__ import annotations

from typing import Any

import pytest
from tranibor_core import TrackerStateMapping

from .conftest import Fixture

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self) -> None:
        self.mappings: list[TrackerStateMapping] = []

    async def set_state(self, key: str, mapping: TrackerStateMapping) -> None:
        self.mappings.append(mapping)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from tranibor_orchestrator.activities import tracker as module

    recording = _Recorder()
    monkeypatch.setattr(module, "tracker_for", lambda *_: recording)
    return recording


async def _mirror(setup: Fixture, state: str) -> None:
    from tranibor_orchestrator.activities.tracker import mirror_state

    payload: dict[str, Any] = {"project_id": setup.project_id, "work_item_id": setup.work_item_id}
    await mirror_state({**payload, "state": state, "reason": "t"})


async def test_a_state_the_workflow_does_not_label_is_written_under_the_new_prefix(
    setup: Fixture, recorder: _Recorder
) -> None:
    """The work item stands in `inbox` (labelled `tranibor:inbox` by its workflow); `triage` is no state of
    the workflow, so it is labelled `tranibor:triage`, and the previous label goes under either prefix."""
    await _mirror(setup, "triage")
    (mapping,) = recorder.mappings
    assert mapping.label == "tranibor:triage"
    assert mapping.remove_labels == ["choregos:inbox", "choregos:triage", "tranibor:inbox"]


async def test_a_replayed_state_change_asks_for_the_same_labels(setup: Fixture, recorder: _Recorder) -> None:
    await _mirror(setup, "inbox")
    await _mirror(setup, "inbox")
    first, again = recorder.mappings
    assert (
        first
        == again
        == TrackerStateMapping(status="Todo", label="tranibor:inbox", remove_labels=["choregos:inbox"])
    )


async def test_coming_back_removes_the_label_of_the_state_left(setup: Fixture, recorder: _Recorder) -> None:
    await _mirror(setup, "triage")
    await _mirror(setup, "inbox")
    assert recorder.mappings[-1].label == "tranibor:inbox"
    assert recorder.mappings[-1].remove_labels == ["choregos:inbox", "choregos:triage", "tranibor:triage"]


async def test_a_state_that_sets_no_label_removes_none_and_a_workflow_s_own_label_is_kept() -> None:
    from tranibor_orchestrator.activities.tracker import _stale_state_labels

    assert _stale_state_labels(None, "tranibor:inbox") == []
    assert _stale_state_labels("agent-ready", "choregos:inbox") == ["choregos:inbox", "tranibor:inbox"]
    assert _stale_state_labels("tranibor:done", "agent-ready") == ["choregos:done"]
