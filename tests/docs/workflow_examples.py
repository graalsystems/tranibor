# SPDX-License-Identifier: Apache-2.0
"""A workflow example of the documentation holds in the deployment that reads it (ADR 0181, round D).

The pages show the software delivery workflow: its gates, its `open_pr`/`merge_pr` and its release train
are the `software-delivery` plugin's. With the plugin's extension installed an example validates with no
error and no warning. Without it the same example is refused, and the refusal is the expected outcome only
if it is exactly the platform naming the plugin to install: every error is "served (or carried) by the
plugin `<p>`, whose extension is not installed", that plugin's package is really absent, and a warning, if
any, sits on a transition the refusal names (a merge whose diff gates are not registered here). Any other
error or warning is a page that drifted, in both deployments.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any

from tranibor_core import parse_workflow

#: How the core refuses a gate, a step effect or a carrier a plugin serves when its extension is absent.
PLUGIN_REFUSAL = re.compile(
    r"(?:served|carried) by the plugin `([a-z0-9-]+)`, whose extension is not installed"
)
#: The first-party plugins' packages, by plugin name.
PLUGIN_PACKAGES = {
    "software-delivery": "tranibor_software_delivery",
    "investment-research": "tranibor_investment_research",
}
_TRANSITION = re.compile(r"^transitions\[(\d+)\]")


def plugin_installed(plugin: str) -> bool:
    """Whether a first-party plugin's package is installed in this environment."""
    return importlib.util.find_spec(PLUGIN_PACKAGES[plugin]) is not None


def _transition(issue: Any) -> str | None:
    found = _TRANSITION.match(str(issue.path or ""))
    return found.group(1) if found else None


def assert_the_workflow_example_holds(name: str, body: str) -> None:
    _workflow, report = parse_workflow(body, strict=False)
    refusals = [issue for issue in report.errors if PLUGIN_REFUSAL.search(issue.message)]
    if not refusals:
        assert report.valid, f"{name}: " + " · ".join(i.format() for i in report.errors)
        assert not report.warnings, f"{name}: " + " · ".join(i.format() for i in report.warnings)
        return
    plugins = {match.group(1) for issue in refusals if (match := PLUGIN_REFUSAL.search(issue.message))}
    installed = sorted(plugin for plugin in plugins if plugin_installed(plugin))
    assert not installed, f"{name}: refused for plugins that are installed: {installed}"
    others = [issue for issue in report.errors if issue not in refusals]
    assert not others, f"{name}: " + " · ".join(i.format() for i in others)
    refused = {_transition(issue) for issue in refusals} - {None}
    stray = [warning for warning in report.warnings if _transition(warning) not in refused]
    assert not stray, f"{name}: " + " · ".join(i.format() for i in stray)
