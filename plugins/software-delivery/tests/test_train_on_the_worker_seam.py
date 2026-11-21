"""The release train is the `software-delivery` plugin's durable code, on the orchestrator's workers through
the worker seam (ADR 0181, round D).

Its workflow and activities left `tranibor_orchestrator` for `tranibor_software_delivery.train`, under the
names every recorded history and every running train carry: `ReleaseTrain`, its signals and queries, and
the sixteen activity names. The worker registers them because the extension declared them; without the
extension it does not, and a train's history no longer replays.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tranibor_core import worker_parts
from tranibor_software_delivery import NAME
from tranibor_software_delivery.extension import provide_worker_parts, register

ROOT = Path(__file__).resolve().parents[3]
HISTORIES = ROOT / "tests" / "replay" / "histories"
ORCHESTRATOR = ROOT / "apps" / "orchestrator" / "src" / "tranibor_orchestrator"

#: The names the orchestrator registered them under, recorded in the `train-*.json` histories.
TRAIN_ACTIVITIES = {
    "load_train_config",
    "check_window",
    "create_release",
    "promote",
    "run_smoke",
    "soak",
    "request_approval",
    "promote_canary_step",
    "verify_prod",
    "finish_release",
    "confirm_auto_sync",
    "confirmer_auto_sync",  # lang: fr-ok (a recorded name, S34-02)
    "rollback",
    "mark_release",
    "apply_terraform",
    "train_stats",
    # The interpreter's boarding of a `via: release_train` transition (S83-05h).
    "signal_train",
}


@pytest.fixture
def without_the_extension() -> Iterator[None]:
    worker_parts.forget(NAME)
    try:
        yield
    finally:
        register()


def _names(workflows: list[object]) -> set[str]:
    from tranibor_orchestrator.extension_parts import workflow_name

    return {workflow_name(cls) for cls in workflows}


def test_the_extension_declares_the_train_under_its_recorded_names() -> None:
    from tranibor_orchestrator.queues import activity_name

    assert NAME in worker_parts.declared_worker_parts()
    parts = provide_worker_parts()
    assert _names(list(parts.workflows)) == {"ReleaseTrain"}
    assert {activity_name(a) for a in parts.activities} == TRAIN_ACTIVITIES
    assert dict(parts.queues) == {}  # every train activity runs on `orchestrator`, as it did


def test_the_worker_carries_the_train_and_the_core_registers_none_of_it() -> None:
    from tranibor_orchestrator.activities import ALL_ACTIVITIES
    from tranibor_orchestrator.extension_parts import all_activities, all_workflows, extension_workflow_names
    from tranibor_orchestrator.queues import activity_name
    from tranibor_orchestrator.workflows import ALL_WORKFLOWS

    assert extension_workflow_names()["ReleaseTrain"] == NAME
    assert "ReleaseTrain" in _names(all_workflows()) and "ReleaseTrain" not in _names(ALL_WORKFLOWS)
    assert {activity_name(a) for a in all_activities()} >= TRAIN_ACTIVITIES
    assert not TRAIN_ACTIVITIES & {activity_name(a) for a in ALL_ACTIVITIES}
    assert not (ORCHESTRATOR / "workflows" / "train.py").exists()
    assert not (ORCHESTRATOR / "activities" / "train.py").exists()


def test_loading_the_extension_imports_none_of_the_train() -> None:
    """The API loads the extensions too: it must not import the train's Temporal code."""
    probe = (
        "import sys\n"
        "from tranibor_software_delivery.extension import register\n"
        "register()\n"
        "train = 'tranibor_software_delivery.train.'\n"
        "loaded = [m for m in sys.modules if m in {train + 'workflow', train + 'activities'}]\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, cwd=ROOT)


def _a_train_history() -> object:
    from temporalio.client import WorkflowHistory

    path = HISTORIES / "train-express-S22-17.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return WorkflowHistory.from_json(payload.get("workflowId", path.stem), payload)


async def test_a_train_history_replays_on_the_workers_with_the_extension() -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    await Replayer(workflows=all_workflows()).replay_workflow(_a_train_history())  # type: ignore[arg-type]


async def test_without_the_extension_a_train_history_has_no_workflow_to_replay(
    without_the_extension: None,
) -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    assert "ReleaseTrain" not in _names(all_workflows())
    result = await Replayer(workflows=all_workflows()).replay_workflow(
        _a_train_history(),  # type: ignore[arg-type]
        raise_on_replay_failure=False,
    )
    assert result.replay_failure is not None
