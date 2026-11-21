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
"""

from __future__ import annotations

from collections.abc import Mapping

#: The carriers a plugin serves once its extension is installed, by name: what the core can say of a
#: carrier it does not know, so that a workflow naming one is refused at publication naming the plugin.
PLUGIN_CARRIERS: Mapping[str, str] = {"release_train": "software-delivery"}

#: The activity the interpreter calls to board a carrier: the name recorded histories carry. The
#: carrier's plugin registers an activity of that name on the workers (the worker seam).
BOARD_ACTIVITY: Mapping[str, str] = {"release_train": "signal_train"}

#: carrier -> the plugin whose extension declared it.
_CARRIERS: dict[str, str] = {}


def declare_carrier(name: str, *, plugin: str) -> None:
    """Called by an extension when it loads. Idempotent for the same plugin; another plugin's carrier, or
    a carrier the DSL does not know, is refused."""
    if name not in BOARD_ACTIVITY:
        raise ValueError(f"`via: {name}` is not a carrier the workflow DSL knows ({sorted(BOARD_ACTIVITY)})")
    owner = _CARRIERS.get(name)
    if owner is not None and owner != plugin:
        raise ValueError(f"the carrier `{name}` is already declared by the plugin `{owner}`")
    _CARRIERS[name] = plugin


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
