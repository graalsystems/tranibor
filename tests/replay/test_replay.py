"""Replay archived histories: a logic change must not break the runs in flight.

This is the counterpart of `workflow.patched()`: the CI replays the recorded histories
against the current code. If a workflow makes a different decision, the replay fails —
before that happens in production.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

HISTORIES = Path(__file__).parent / "histories"

#: The workflow types a first-party plugin's extension registers through the worker seam (ADR 0181,
#: round D), and the plugin. Their histories replay wherever the extension is installed (the suite's own
#: environment); in the core alone (`TRANIBOR_TEST_CORE_ALONE=1`, the `core-without-plugins` job) they
#: are skipped, naming the plugin, and every other history still replays.
PLUGIN_WORKFLOWS = {"ReleaseTrain": "software-delivery"}
CORE_ALONE = os.environ.get("TRANIBOR_TEST_CORE_ALONE") == "1"


def history_files() -> list[Path]:
    return sorted(HISTORIES.glob("*.json"))


def recorded_workflow_types() -> set[str]:
    """The workflow type each archived history starts with."""
    return {
        json.loads(path.read_text(encoding="utf-8"))["events"][0]["workflowExecutionStartedEventAttributes"][
            "workflowType"
        ]["name"]
        for path in history_files()
    }


def registered_workflow_types() -> set[str]:
    """The name Temporal knows each registered workflow class by (`@workflow.defn(name=...)`)."""
    from tranibor_orchestrator.extension_parts import all_workflows

    names: set[str] = set()
    for workflow_class in all_workflows():
        definition = getattr(workflow_class, "__temporal_workflow_definition", None)
        assert definition is not None, f"{workflow_class!r} is in all_workflows() but is no workflow"
        names.add(definition.name)
    return names


def workflows_without_history(recorded: Iterable[str], expected: Iterable[str]) -> set[str]:
    """The expected workflow types that no recorded history starts with."""
    return set(expected) - set(recorded)


def history_type(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["events"][0]["workflowExecutionStartedEventAttributes"]["workflowType"]["name"])


def test_every_archived_history_has_its_workflow_registered() -> None:
    """A history whose workflow no worker registers replays nothing: in the suite's environment every
    type is registered, the plugins' through their extension."""
    registered = registered_workflow_types()
    missing = {history_type(path) for path in history_files()} - registered
    if CORE_ALONE:
        assert missing <= set(PLUGIN_WORKFLOWS), f"neither the core's nor a plugin's: {sorted(missing)}"
        return
    assert not missing, f"archived histories of a workflow nothing registers: {sorted(missing)}"


def test_the_plugins_workflows_are_registered_by_their_extension() -> None:
    if CORE_ALONE:
        pytest.skip("the core alone: no plugin's extension is installed")
    from tranibor_orchestrator.extension_parts import extension_workflow_names

    owners = extension_workflow_names()
    assert {name: owners.get(name) for name in PLUGIN_WORKFLOWS} == PLUGIN_WORKFLOWS


def test_every_workflow_has_a_history() -> None:
    """A workflow with no archived history is a workflow whose change nothing replays: record one
    (`make replay-record`, or the `__main__` block of its `apps/orchestrator/tests/test_*_history.py`)."""
    missing = workflows_without_history(recorded_workflow_types(), registered_workflow_types())
    assert not missing, f"no archived history in tests/replay/histories for: {sorted(missing)}"


def test_a_workflow_without_a_history_is_reported() -> None:
    """The guard's own logic, on synthetic types: what has no history is named, what has one is not."""
    recorded = ["WorkflowInterpreter", "ReleaseTrain", "ReleaseTrain"]
    expected = ["WorkflowInterpreter", "ReleaseTrain", "AgentTrial", "MemoryIngestion"]
    assert workflows_without_history(recorded, expected) == {"AgentTrial", "MemoryIngestion"}
    assert workflows_without_history(expected, expected) == set()
    assert workflows_without_history([], expected) == set(expected)


@pytest.mark.skipif(not history_files(), reason="no archived history (see `make replay-record`)")
@pytest.mark.parametrize("path", history_files(), ids=lambda path: path.stem)
async def test_replay_history(path: Path) -> None:
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    payload = json.loads(path.read_text(encoding="utf-8"))
    kind = history_type(path)
    if CORE_ALONE and kind in PLUGIN_WORKFLOWS:
        pytest.skip(f"`{kind}` is the `{PLUGIN_WORKFLOWS[kind]}` plugin's, whose extension is not installed")
    history = WorkflowHistory.from_json(payload.get("workflowId", path.stem), payload)
    replayer = Replayer(workflows=all_workflows())
    await replayer.replay_workflow(history)


async def test_record_and_replay_roundtrip(tmp_path: Path) -> None:
    """Record a fresh history then replay it: the loop is verifiable in CI."""
    from temporalio.client import WorkflowHistory
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows
    from tranibor_orchestrator.testing import workers_by_queue

    ReleaseTrain = pytest.importorskip(  # noqa: N806 - the class, by its name
        "tranibor_software_delivery.train.workflow", reason="the release train is the plugin's"
    ).ReleaseTrain

    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        # One worker per queue: `notify` and `enqueue_merge` are scheduled on `tracker`, not on
        # the workflow's queue. A single worker would hide the split the history must carry,
        # and the replayed history would be that of a deployment that does not exist.
        async with workers_by_queue(
            environment.client,
            activities=_stub_activities(),
            workflows=[ReleaseTrain],
            workflow_queue="replay",
        ):
            handle = await environment.client.start_workflow(
                "ReleaseTrain",
                {"project_slug": "replay", "env": "dev"},
                id="train-replay-dev",
                task_queue="replay",
            )
            await handle.signal("abort", {"by": "test"})
            await handle.result()
            history = await handle.fetch_history()
    finally:
        await environment.shutdown()

    archived = tmp_path / "train.json"
    archived.write_text(history.to_json(), encoding="utf-8")

    replayer = Replayer(workflows=all_workflows())
    await replayer.replay_workflow(
        WorkflowHistory.from_json("train-replay-dev", json.loads(archived.read_text()))
    )


def _stub_activities() -> list[Any]:
    """Neutral activities: the replay does not replay effects, only decisions."""
    from temporalio import activity

    @activity.defn(name="load_train_config")
    async def load_train_config(payload: dict[str, Any]) -> dict[str, Any]:
        return {"mode": "auto_sync"}

    return [load_train_config]
