# SPDX-License-Identifier: Apache-2.0
"""The worker seam (ADR 0181, round D): the durable code an extension adds to the orchestrator's workers.

A trade whose work outlives a request (a release train that waits for a window, a deployment that soaks)
needs Temporal workflows and activities. Until round D the core's workers carried only the core's, so the
`software-delivery` plugin's train had to live in the orchestrator. An extension now declares its own from
its `choregos.plugins` entry point:

    declare_worker_parts("software-delivery", provide)

`provide` is a callable with no argument that returns `WorkerParts`. Only the worker calls it, when it
builds its workers (and the test and replay tools that stand for it): the API, which loads the same
extensions, never imports an extension's Temporal code, and neither does a process that only validates
workflows.

The names are the history's: a workflow is known to Temporal by `@workflow.defn(name=...)` and an
activity by `@activity.defn(name=...)`, and those are recorded in every history, so an extension that
takes over a workflow the core used to carry keeps its names, and every archived history replays with the
extension installed. **A name the core or another extension already registers stops the worker**
(`tranibor_orchestrator.extension_parts`): Temporal would serve one of the two without a word.

Two rules an extension's code follows, both learnt the hard way (`test_worker_seam.py`):

- its entry point imports none of its Temporal code; `provide` does, when the worker calls it. The worker's
  own modules load the extensions when they are imported, so an entry point that imported them back would
  be a circular import;
- a workflow module imports the orchestrator's helpers (`tranibor_orchestrator.workflows.scheduling`,
  `activity_names`) inside `workflow.unsafe.imports_passed_through()`, as the core's workflows do: imported
  inside the sandbox, the orchestrator's package pulls in its activities, which the sandbox refuses.

This module depends on the standard library only: an extension's `register()` imports it in every process
that loads extensions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkerParts:
    """What an extension adds to the workers.

    - `workflows`: workflow classes, registered on the `orchestrator` queue, where every workflow runs;
    - `activities`: activity functions, registered on the `orchestrator` queue (which carries everything)
      and on the queue `queues` names for them;
    - `queues`: activity name -> the specialised queue that carries it (`executor`, `tracker`, `memory`).
      An activity absent from it runs on the queue of the workflow that calls it.
    """

    workflows: Sequence[Any] = ()
    activities: Sequence[Any] = ()
    queues: Mapping[str, str] = field(default_factory=dict)


Provide = Callable[[], WorkerParts]

_PROVIDERS: dict[str, Provide] = {}


def declare_worker_parts(plugin: str, provide: Provide) -> None:
    """Called by an extension when it loads. Idempotent: a second call for the same plugin replaces the
    first (the API loads the extensions twice)."""
    if not plugin or not isinstance(plugin, str):
        raise ValueError("a plugin's worker parts are declared under the plugin's name")
    if not callable(provide):
        raise TypeError(f"`provide` is a callable that returns WorkerParts, not {type(provide).__name__}")
    _PROVIDERS[plugin] = provide


def declared_worker_parts() -> dict[str, Provide]:
    """plugin -> its `provide`, in the order the extensions declared them."""
    return dict(_PROVIDERS)


def forget(plugin: str) -> None:
    """Drop what `plugin` declared (a test that installed an extension does it after)."""
    _PROVIDERS.pop(plugin, None)
