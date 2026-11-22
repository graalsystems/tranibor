# SPDX-License-Identifier: Apache-2.0
"""An installed extension adds its own Temporal workflows and activities to the workers (ADR 0181, round D).

The extension is a real module with its `.dist-info`, loaded by `load_extensions()` as at a worker's
start-up: a test plugin `itsm` declares, through `tranibor_core.worker_parts`, a workflow `IncidentClock`
and an activity `page_on_call` that runs on the `tracker` queue. The workers then carry both, the
workflow schedules the activity where a worker carries it, and a name the core already registers stops the
worker instead of being served twice.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from temporalio import workflow

#: The extension's entry point: it declares its parts and imports none of its Temporal code, which only
#: the worker's `provide` call loads (the worker's own modules load the extensions when they are imported).
EXTENSION = """
from tranibor_core.worker_parts import WorkerParts, declare_worker_parts


def provide():
    from extension_clock_parts import IncidentClock, page_on_call

    return WorkerParts(
        workflows=[IncidentClock], activities=[page_on_call], queues={"page_on_call": "tracker"}
    )


def plug_in():
    declare_worker_parts("itsm", provide)
"""

#: Its durable code.
PARTS = """
from datetime import timedelta

from temporalio import activity, workflow

# The orchestrator's helpers pass through the sandbox, as the core's workflows import them.
with workflow.unsafe.imports_passed_through():
    from tranibor_orchestrator.workflows.scheduling import execute_on_its_queue

PAGED = []


@activity.defn(name="page_on_call")
async def page_on_call(incident: str) -> str:
    PAGED.append(incident)
    return f"paged for {incident}"


@workflow.defn(name="IncidentClock")
class IncidentClock:
    @workflow.run
    async def run(self, incident: str) -> str:
        return await execute_on_its_queue(
            page_on_call, incident, start_to_close_timeout=timedelta(seconds=10)
        )
"""


@pytest.fixture
def extension(tmp_path: pathlib.Path) -> Iterator[Any]:
    from tranibor_adapters import load_extensions
    from tranibor_core import worker_parts
    from tranibor_orchestrator import queues

    root = tmp_path / "site"
    root.mkdir()
    (root / "extension_clock.py").write_text(EXTENSION, encoding="utf-8")
    (root / "extension_clock_parts.py").write_text(PARTS, encoding="utf-8")
    info = root / "extension_clock-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: extension_clock\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        "[choregos.plugins]\nextension_clock = extension_clock:plug_in\n", encoding="utf-8"
    )
    sys.path.insert(0, str(root))
    tracker = queues.ACTIVITIES_BY_QUEUE["tracker"]
    try:
        assert "extension_clock" in load_extensions()
        assert "extension_clock_parts" not in sys.modules  # loading the extension imports no Temporal code
        import extension_clock_parts

        yield extension_clock_parts
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("extension_clock", None)
        sys.modules.pop("extension_clock_parts", None)
        worker_parts.forget("itsm")
        queues.ACTIVITIES_BY_QUEUE["tracker"] = tracker
        queues.QUEUE_BY_ACTIVITY.pop("page_on_call", None)


def _names(workflows: list[Any]) -> set[str]:
    from tranibor_orchestrator.extension_parts import workflow_name

    return {workflow_name(cls) for cls in workflows}


def test_the_workers_carry_the_extension_s_workflow_and_activity(extension: Any) -> None:
    from tranibor_orchestrator.extension_parts import all_workflows, extension_workflow_names
    from tranibor_orchestrator.queues import activity_name, queue_of
    from tranibor_orchestrator.worker import activities_for

    assert "IncidentClock" in _names(all_workflows())
    assert extension_workflow_names()["IncidentClock"] == "itsm"
    assert queue_of("page_on_call") == "tracker"
    # The specialised queue carries it, and `orchestrator` too (the net of a version upgrade).
    assert "page_on_call" in {activity_name(a) for a in activities_for("tracker")}
    assert "page_on_call" in {activity_name(a) for a in activities_for("orchestrator")}
    assert "page_on_call" not in {activity_name(a) for a in activities_for("executor")}


def test_without_the_extension_the_workers_carry_only_the_core(extension: Any) -> None:
    from tranibor_core import worker_parts
    from tranibor_orchestrator.extension_parts import all_activities, all_workflows
    from tranibor_orchestrator.queues import activity_name

    worker_parts.forget("itsm")
    assert "IncidentClock" not in _names(all_workflows())
    assert "page_on_call" not in {activity_name(a) for a in all_activities()}


async def test_the_extension_s_workflow_runs_its_activity_on_its_queue(
    extension: Any, temporal_env: Any
) -> None:
    from tranibor_orchestrator.extension_parts import all_activities, all_workflows
    from tranibor_orchestrator.testing import workers_by_queue

    async with workers_by_queue(
        temporal_env.client, activities=all_activities(), workflows=all_workflows()
    ) as by_queue:
        result = await temporal_env.client.execute_workflow(
            "IncidentClock", "INC-7", id="incident-clock-INC-7", task_queue="test"
        )
    assert result == "paged for INC-7"
    assert extension.PAGED == ["INC-7"]
    # The test workers lay no net: the activity ran because the `tracker` worker carries it.
    assert extension.page_on_call in by_queue["tracker"]


def _clashing(parts: Any) -> Any:
    from tranibor_core import worker_parts
    from tranibor_orchestrator.extension_parts import WorkerPartsError, extension_parts

    worker_parts.declare_worker_parts("clash", lambda: parts)
    try:
        with pytest.raises(WorkerPartsError) as refused:
            extension_parts()
    finally:
        worker_parts.forget("clash")
    return str(refused.value)


@workflow.defn(name="WorkflowInterpreter", sandboxed=False)
class Impostor:
    """A workflow an extension would register under the name of the core's interpreter."""

    @workflow.run
    async def run(self) -> None:
        return None


def test_a_workflow_name_the_core_registers_stops_the_worker() -> None:
    from tranibor_core.worker_parts import WorkerParts

    message = _clashing(WorkerParts(workflows=[Impostor]))
    assert "`WorkflowInterpreter`" in message and "the core" in message and "`clash`" in message


def test_an_activity_name_the_core_registers_stops_the_worker() -> None:
    from temporalio import activity
    from tranibor_core.worker_parts import WorkerParts

    @activity.defn(name="notify")
    async def notify() -> None:
        return None

    message = _clashing(WorkerParts(activities=[notify]))
    assert "`notify`" in message and "the core" in message


def test_a_queue_that_does_not_exist_or_an_activity_not_provided_is_refused() -> None:
    from temporalio import activity
    from tranibor_core.worker_parts import WorkerParts

    @activity.defn(name="page_the_moon")
    async def page_the_moon() -> None:
        return None

    assert "no queue" in _clashing(WorkerParts(activities=[page_the_moon], queues={"page_the_moon": "moon"}))
    assert "provides no such activity" in _clashing(WorkerParts(queues={"absent": "tracker"}))


def test_two_extensions_cannot_register_one_name(extension: Any) -> None:
    from tranibor_core.worker_parts import WorkerParts

    message = _clashing(WorkerParts(activities=[extension.page_on_call]))
    assert "`page_on_call`" in message and "`itsm`" in message


def test_what_provide_returns_must_be_worker_parts() -> None:
    message = _clashing({"workflows": []})
    assert "not WorkerParts" in message


def test_the_seam_refuses_a_declaration_it_cannot_resolve() -> None:
    from tranibor_core.worker_parts import declare_worker_parts

    with pytest.raises(ValueError, match="plugin's name"):
        declare_worker_parts("", lambda: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="callable"):
        declare_worker_parts("itsm", "provide")  # type: ignore[arg-type]


def test_a_queue_is_placed_once() -> None:
    from tranibor_orchestrator import queues

    with pytest.raises(ValueError, match="already on"):
        queues.declare_queue("notify", "executor")
    with pytest.raises(ValueError, match="no queue"):
        queues.declare_queue("whatever", "moon")
    queues.declare_queue("notify", "tracker")  # the same placement: nothing to do
    assert queues.ACTIVITIES_BY_QUEUE["tracker"].count("notify") == 1


async def _a_step(params: dict[str, Any], bundle: Any, settings: Any) -> str:
    return f"paged {params.get('who', 'nobody')}"


async def test_an_extension_s_provisioning_step_is_run_by_the_core_s_activity() -> None:
    """A template names a step an extension runs (ADR 0181, round D): `run_provision_step` dispatches it,
    and its remediation is the one shown beside a failure."""
    from tranibor_core import worker_parts
    from tranibor_core.worker_parts import ProvisioningStep, WorkerParts
    from tranibor_orchestrator.activities import provisioning

    step = ProvisioningStep(_a_step, "Page someone else.")
    worker_parts.declare_worker_parts(
        "itsm-steps", lambda: WorkerParts(provisioning_steps={"itsm.page": step})
    )
    try:
        assert await provisioning._execute("itsm.page", {"who": "ops"}, None, None, {}) == "paged ops"
        assert provisioning._remediation("itsm.page") == "Page someone else."
    finally:
        worker_parts.forget("itsm-steps")
    said = await provisioning._execute("itsm.page", {}, None, None, {})
    assert said == "step `itsm.page` skipped (not implemented for this template)"
    assert provisioning._remediation("itsm.page").startswith("Fix the cause")


def test_a_provisioning_step_the_core_runs_stops_the_worker() -> None:
    from tranibor_core.worker_parts import ProvisioningStep, WorkerParts

    message = _clashing(WorkerParts(provisioning_steps={"memory.create_tenant": ProvisioningStep(_a_step)}))
    assert "`memory.create_tenant`" in message and "the core" in message and "`clash`" in message
