# SPDX-License-Identifier: Apache-2.0
"""Template conformance of the software delivery templates (moved from the core's
`tests/conformance/templates/test_templates.py`, S83-05n, ADR 0181 round D).

A template that requires the plugin's `scm`, `ci` or `cd` connector promises connectors, gates, step
effects and a release train that exist only where the plugin is installed: here, it is held to the same
checks as every other shipped template (the test kit's `tranibor_testkit.templates`), and the core's suite
holds the others.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tranibor_testkit.templates import (
    CORE_MANIFESTS,
    MANIFESTS,
    SOFTWARE_DELIVERY_MANIFESTS,
    assert_actions_call_only_what_the_template_announces,
    assert_the_declared_connectors_exist,
    assert_the_template_agents_and_skills_are_installable,
    assert_the_workflows_the_default_the_routing_and_the_policy_exist,
)

pytestmark = pytest.mark.conformance


def test_the_plugin_holds_the_templates_the_core_does_not() -> None:
    names = {path.parent.name for path in SOFTWARE_DELIVERY_MANIFESTS}
    assert {"github-software-delivery", "software-delivery-governed"} <= names
    assert sorted(CORE_MANIFESTS + SOFTWARE_DELIVERY_MANIFESTS) == MANIFESTS


@pytest.mark.parametrize("path", SOFTWARE_DELIVERY_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_declared_connectors_exist(path: Path) -> None:
    assert_the_declared_connectors_exist(path)


@pytest.mark.parametrize("path", SOFTWARE_DELIVERY_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_workflows_the_default_the_routing_and_the_policy_exist(path: Path) -> None:
    assert_the_workflows_the_default_the_routing_and_the_policy_exist(path)


@pytest.mark.parametrize("path", SOFTWARE_DELIVERY_MANIFESTS, ids=lambda p: p.parent.name)
def test_actions_call_only_what_the_template_announces(path: Path) -> None:
    assert_actions_call_only_what_the_template_announces(path)


@pytest.mark.parametrize("path", SOFTWARE_DELIVERY_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_template_agents_and_skills_are_installable(path: Path) -> None:
    assert_the_template_agents_and_skills_are_installable(path)
