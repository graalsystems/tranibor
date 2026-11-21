# SPDX-License-Identifier: Apache-2.0
"""The carrier seam (ADR 0181, round D): a transition a plugin's durable workflow carries (`via:`).

`via: release_train` hands a work item to the `software-delivery` plugin's train: the interpreter boards
it, then waits for the deployment events that say the train delivered (or failed). The train itself is the
plugin's (its workflow and activities run on the workers through the worker seam); what the core keeps is
the interpreter's side, which is part of the `WorkflowInterpreter` histories record:

- the interpreter boards a carrier by calling an activity **by name**, the name every recorded history
  carries (`signal_train` for `release_train`, `BOARD_ACTIVITY`), which the plugin's extension registers.
  The interpreter never reads this registry, so a history replays without the extension;
- the validator refuses at publication a `via:` no installed extension carries, naming the plugin
  (`transition.carrier_unavailable`), as it refuses a gate or a step effect; a reader that is not the
  deployment leaves it to the deployment (`deployment_issues`).

The DSL's `via` takes one value today (`release_train`, `workflow.schema.json`): a new carrier is a
contract change, and its board activity is named here.

What the interpreter hands the carrier when it boards (`boarding_env`, `boarding_approval`) is read from
the transition's `train:` section, in deterministic workflow code that replays without the extension: it
is the interpreter's side of the seam, and stays here (ADR 0181, round D, S83-05k). What only the plugin
asks of a workflow (the train a merged work item takes) is the plugin's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from tranibor_contracts import Workflow
    from tranibor_contracts.workflow import Transition

#: The carriers a plugin serves once its extension is installed, by name: what the core can say of a
#: carrier it does not know, so that a workflow naming one is refused at publication naming the plugin.
PLUGIN_CARRIERS: Mapping[str, str] = {"release_train": "software-delivery"}

#: The activity the interpreter calls to board a carrier: the name recorded histories carry. The
#: carrier's plugin registers an activity of that name on the workers (the worker seam).
BOARD_ACTIVITY: Mapping[str, str] = {"release_train": "signal_train"}

#: carrier -> the plugin whose extension declared it.
_CARRIERS: dict[str, str] = {}
#: carrier -> the connector kinds a project needs for a work item to take it (its requirements).
_NEEDS: dict[str, tuple[str, ...]] = {}


def declare_carrier(name: str, *, plugin: str, needs: Iterable[str] = ()) -> None:
    """Called by an extension when it loads. Idempotent for the same plugin; another plugin's carrier, or
    a carrier the DSL does not know, is refused. `needs`: the connector kinds a project needs to send a
    work item through it (the train's `cd`), which its workflows' requirements then list."""
    if name not in BOARD_ACTIVITY:
        raise ValueError(f"`via: {name}` is not a carrier the workflow DSL knows ({sorted(BOARD_ACTIVITY)})")
    owner = _CARRIERS.get(name)
    if owner is not None and owner != plugin:
        raise ValueError(f"the carrier `{name}` is already declared by the plugin `{owner}`")
    _CARRIERS[name] = plugin
    _NEEDS[name] = tuple(needs)


def carrier_of(transition: Transition) -> str | None:
    """The carrier a transition hands its work item to: its `via:`, or `release_train` for a transition
    written with a `train:` section alone (as the shipped workflows were)."""
    if transition.via is not None:
        return str(transition.via)
    return "release_train" if transition.train is not None else None


def carrier_needs(name: str) -> tuple[str, ...]:
    """The connector kinds a project needs for this carrier; nothing for a carrier no extension declares
    (the validator refuses it at publication, naming its plugin)."""
    return _NEEDS.get(name, ())


class TrainApproval(TypedDict):
    """What a work item requires of its batch's departure: a dict, because it travels in Temporal signals."""

    required: bool
    group: str | None


def boarding_env(transition: Transition) -> str:
    """The target environment of a carried transition; `prod` when it does not say (`via:` alone)."""
    return transition.train.env if transition.train else "prod"


def boarding_approval(workflow: Workflow, transition: Transition) -> TrainApproval:
    """`train.approval` names a human actor: their group approves the departure. An actor that is not
    human still requires the approval; the group will then be the policy's."""
    from tranibor_contracts import HumanActor

    if transition.train is None or not transition.train.approval:
        return {"required": False, "group": None}
    actor = workflow.actors.get(str(transition.train.approval))
    return {"required": True, "group": actor.group if isinstance(actor, HumanActor) else None}


def known_carriers() -> list[str]:
    return sorted(_CARRIERS)


def unavailable_carrier(name: str) -> str | None:
    """Why this deployment cannot carry `via: <name>`, naming the plugin to install; `None` when it can."""
    if name in _CARRIERS:
        return None
    plugin = PLUGIN_CARRIERS.get(name)
    if plugin is not None:
        return (
            f"`via: {name}` is carried by the plugin `{plugin}`, whose extension is not installed in this "
            "deployment: install it, or remove the transition"
        )
    return f"unknown carrier: `via: {name}`: no installed extension carries it"


def forget(plugin: str) -> None:
    """Drop what `plugin` declared (a test that installed an extension does it after)."""
    for name in [name for name, owner in _CARRIERS.items() if owner == plugin]:
        del _CARRIERS[name]
        _NEEDS.pop(name, None)
