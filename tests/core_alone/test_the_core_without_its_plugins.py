# SPDX-License-Identifier: Apache-2.0
"""The core runs with the first-party plugins' extensions uninstalled (ADR 0181, round D).

The `core-without-plugins` CI job builds the environment without `tranibor-software-delivery` and
`tranibor-investment-research` (`uv sync --no-install-package ...`) and sets `TRANIBOR_TEST_CORE_ALONE=1`.
These tests then check that the environment really lacks them (a job that installed them by mistake would
prove nothing), that the API and the worker start, and that what the plugins serve is refused naming the
plugin instead of failing at random. Anywhere else they are skipped: the suite's own environment installs
both extensions.
"""

from __future__ import annotations

import importlib.util
import os
from importlib import metadata

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TRANIBOR_TEST_CORE_ALONE") != "1",
    reason="the core alone: the `core-without-plugins` CI job (TRANIBOR_TEST_CORE_ALONE=1)",
)

#: The first-party plugins whose extensions the job leaves out: entry point -> import package.
LEFT_OUT = {
    "software-delivery": "tranibor_software_delivery",
    "investment-research": "tranibor_investment_research",
}

WORKFLOW = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: delivery, version: 1}
actors:
  dev: {type: agent, role: implement}
states:
  todo: {display: To do, kind: wait}
  done: {display: Done, terminal: true}
transitions:
  - {id: t-do, from: todo, to: done, by: dev, gates: [{name: ci_green}]}
"""


def test_the_environment_really_lacks_the_plugins_extensions() -> None:
    points = {point.name for point in metadata.entry_points(group="choregos.plugins")}
    for name, package in LEFT_OUT.items():
        assert name not in points, f"the extension `{name}` is installed: this job proves nothing"
        assert importlib.util.find_spec(package) is None, package
    from tranibor_adapters import load_extensions

    assert not set(load_extensions()) & set(LEFT_OUT)


def test_the_api_starts() -> None:
    from tranibor_api.main import create_app

    app = create_app()
    paths = set(app.openapi()["paths"])
    assert "/api/v1/orgs/{org}/projects" in paths and "/api/v1/projects/{id}/actions" in paths
    # The release train's routes are the `software-delivery` plugin's (round D, S83-05c).
    assert not [path for path in paths if "/trains/" in path or path.startswith("/api/v1/releases/")]
    # A run's diff and the DORA metrics too (S83-05e).
    assert not {"/api/v1/runs/{id}/diff", "/api/v1/projects/{id}/metrics/dora"} & paths


def test_the_worker_builds_its_workflows_and_activities_without_them() -> None:
    from tranibor_core.worker_parts import declared_worker_parts
    from tranibor_orchestrator.extension_parts import (
        all_activities,
        all_workflows,
        extension_parts,
        workflow_name,
    )
    from tranibor_orchestrator.worker import activities_for

    assert not set(declared_worker_parts()) & set(LEFT_OUT)
    assert extension_parts() == []
    assert all_workflows() and all_activities()
    # The release train is the `software-delivery` plugin's (round D, S83-05b).
    assert "ReleaseTrain" not in {workflow_name(cls) for cls in all_workflows()}
    assert activities_for("orchestrator") and activities_for("tracker")


def test_a_gate_of_the_plugin_is_refused_naming_it() -> None:
    from tranibor_core import parse_workflow
    from tranibor_core.gates import known_gates, unavailable

    assert "ci_green" not in known_gates()
    why = unavailable("ci_green")
    assert why is not None and "`software-delivery`" in why
    _, report = parse_workflow(WORKFLOW, strict=False)
    assert [e.code for e in report.errors if "software-delivery" in e.message] == ["gate.unknown"]


def test_a_project_with_a_repository_and_no_template_gets_a_workflow_the_core_runs() -> None:
    """`default-simple` is the plugin's to run (its gates, `open_pr`, `merge_pr`): the core gives
    `default-request` instead (S83-05d)."""
    from tranibor_api.services.templates import delivery

    [source] = delivery({}, None, has_repository=True).workflows
    workflow, report = parse_workflow_report(source)
    assert workflow == "default-request" and report


def parse_workflow_report(source: str) -> tuple[str, bool]:
    from tranibor_core import parse_workflow

    parsed, report = parse_workflow(source, strict=False)
    return parsed.metadata.name, report.valid


def test_the_api_emits_none_of_the_plugins_series() -> None:
    """The release train's series are the plugin's (S83-05f): the core knows them by name only."""
    from tranibor_api.metrics import PLUGIN_SERIES, emitted_names

    assert PLUGIN_SERIES and not set(PLUGIN_SERIES) & emitted_names()
