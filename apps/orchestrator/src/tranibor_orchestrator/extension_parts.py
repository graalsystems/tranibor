# SPDX-License-Identifier: Apache-2.0
"""The worker side of the worker seam (ADR 0181, round D): the core's workflows and activities, plus
those the installed extensions declared through `tranibor_core.worker_parts`.

`all_workflows()` and `all_activities()` are what a worker registers, what the replayer replays with and
what the test workers carry. Resolving an extension's parts also places its activities on their queues
(`queues.declare_queue`), so that `execute_on_its_queue` schedules them where a worker carries them.

**A clash stops the worker**, naming both owners: a workflow or an activity name the core or another
extension already registers, or an activity placed on a queue that does not exist or on two queues.
Temporal registers one implementation per name; the other would be ignored without a word, and a recorded
history could replay against code its run never ran.
"""

from __future__ import annotations

from typing import Any

from tranibor_core.worker_parts import ProvisioningStep, WorkerParts, declared_worker_parts

from .queues import ACTIVITIES_BY_QUEUE, activity_name, declare_queue


class WorkerPartsError(RuntimeError):
    """An extension's worker parts cannot be registered beside the others."""


def workflow_name(workflow_class: Any) -> str:
    """The name Temporal knows a workflow class by (`@workflow.defn(name=...)`)."""
    definition = getattr(workflow_class, "__temporal_workflow_definition", None)
    name = getattr(definition, "name", None)
    if not isinstance(name, str):
        raise WorkerPartsError(f"{workflow_class!r} is no Temporal workflow (`@workflow.defn` is missing)")
    return name


def _core() -> tuple[list[Any], list[Any]]:
    from .activities import ALL_ACTIVITIES
    from .workflows import ALL_WORKFLOWS, WORKFLOW_ACTIVITIES

    return list(ALL_WORKFLOWS), [*ALL_ACTIVITIES, *WORKFLOW_ACTIVITIES]


def extension_parts() -> list[tuple[str, WorkerParts]]:
    """(plugin, its parts) for every extension that declared some, checked against the core and each
    other, with their activities placed on their queues."""
    workflows, activities = _core()
    workflow_owner = {workflow_name(cls): "the core" for cls in workflows}
    activity_owner: dict[str, str] = {}
    for function in activities:
        name = activity_name(function)
        if name is not None:
            activity_owner[name] = "the core"
    from .activities.provisioning import CORE_STEPS

    step_owner = dict.fromkeys(CORE_STEPS, "the core")
    resolved: list[tuple[str, WorkerParts]] = []
    for plugin, provide in declared_worker_parts().items():
        parts = provide()
        if not isinstance(parts, WorkerParts):
            raise WorkerPartsError(
                f"the extension `{plugin}` provides {type(parts).__name__}, not WorkerParts"
            )
        owner = f"the extension `{plugin}`"
        for cls in parts.workflows:
            name = workflow_name(cls)
            if name in workflow_owner:
                raise WorkerPartsError(
                    f"the workflow `{name}` of {owner} is already registered by {workflow_owner[name]}"
                )
            workflow_owner[name] = owner
        names: set[str] = set()
        for function in parts.activities:
            name = activity_name(function)
            if name is None:
                raise WorkerPartsError(f"{function!r} of {owner} is no Temporal activity (`@activity.defn`)")
            if name in activity_owner:
                raise WorkerPartsError(
                    f"the activity `{name}` of {owner} is already registered by {activity_owner[name]}"
                )
            activity_owner[name] = owner
            names.add(name)
        for name, queue in parts.queues.items():
            if name not in names:
                raise WorkerPartsError(f"{owner} places `{name}` on `{queue}`, but provides no such activity")
            if queue not in ACTIVITIES_BY_QUEUE:
                raise WorkerPartsError(
                    f"{owner} places `{name}` on `{queue}`, which is no queue ({sorted(ACTIVITIES_BY_QUEUE)})"
                )
            declare_queue(name, queue)
        for step in parts.provisioning_steps:
            if step in step_owner:
                raise WorkerPartsError(
                    f"the provisioning step `{step}` of {owner} is already run by {step_owner[step]}"
                )
            step_owner[step] = owner
        resolved.append((plugin, parts))
    return resolved


def all_workflows() -> list[Any]:
    """The core's workflows, then the extensions'."""
    workflows, _ = _core()
    for _, parts in extension_parts():
        workflows.extend(parts.workflows)
    return workflows


def all_activities() -> list[Any]:
    """The core's activities (those of `activities` and those the workflows call directly), then the
    extensions', then the core's stand-ins for the names no extension provides
    (`activities.stand_ins`)."""
    from .activities.stand_ins import STAND_INS

    _, activities = _core()
    provided: set[str] = set()
    for _, parts in extension_parts():
        activities.extend(parts.activities)
        provided.update(name for name in map(activity_name, parts.activities) if name is not None)
    activities.extend(stand_in for stand_in in STAND_INS if activity_name(stand_in) not in provided)
    return activities


def extension_provisioning_steps() -> dict[str, ProvisioningStep]:
    """step name -> the step an extension runs for a template's provisioning."""
    return {name: step for _, parts in extension_parts() for name, step in parts.provisioning_steps.items()}


def extension_workflow_names() -> dict[str, str]:
    """workflow name -> the plugin whose extension registers it."""
    return {workflow_name(cls): plugin for plugin, parts in extension_parts() for cls in parts.workflows}
