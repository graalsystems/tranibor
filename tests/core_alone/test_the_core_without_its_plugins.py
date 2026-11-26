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
    points = {
        point.name
        for group in ("tranibor.plugins", "choregos.plugins")
        for point in metadata.entry_points(group=group)
    }
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


def test_the_core_has_no_scm_ci_or_cd_connector_and_says_whose_they_are() -> None:
    """The `scm`, `ci` and `cd` connector types and slots are the plugin's (S83-05i, S83-05j): the core
    registers none, its adapter set has none, and reading one names the plugin."""
    from tranibor_adapters import AdapterSet, available, connector_types
    from tranibor_adapters.registry import adapter_slots, default_type

    assert not [pair for pair in connector_types() if pair[0] in {"scm", "ci", "cd"}]
    assert available("scm") == available("ci") == available("cd") == []
    assert adapter_slots() == {} and default_type("ci") is None and default_type("scm") is None
    adapters = AdapterSet.fakes()
    assert adapters.extra == {}
    with pytest.raises(AttributeError, match="the plugin `software-delivery`'s"):
        adapters.slot("cd")
    with pytest.raises(AttributeError, match="the plugin `software-delivery`'s"):
        _ = adapters.ci
    with pytest.raises(AttributeError, match="the plugin `software-delivery`'s"):
        _ = adapters.scm


def test_the_api_serves_none_of_the_pipelines_and_deployments_webhooks() -> None:
    from tranibor_api.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/api/v1/webhooks/github" in paths
    assert not {"/api/v1/webhooks/tekton", "/api/v1/webhooks/jenkins", "/api/v1/webhooks/argocd"} & paths


async def test_a_provisioning_step_of_the_plugin_is_skipped_saying_so() -> None:
    from tranibor_orchestrator.activities.provisioning import _execute
    from tranibor_orchestrator.extension_parts import extension_provisioning_steps

    assert extension_provisioning_steps() == {}
    said = await _execute("argocd.wait_synced", {}, None, None, {})
    assert said == "step `argocd.wait_synced` skipped (not implemented for this template)"


def test_the_worker_answers_a_run_s_diff_with_the_core_s_stand_in() -> None:
    """`collect_run_artifacts` (called by name after every agent step) and the source control's activities
    are the plugin's (S83-05j): the core registers none of them but the stand-in, which files nothing."""
    from tranibor_orchestrator.activities.stand_ins import collect_run_artifacts
    from tranibor_orchestrator.extension_parts import all_activities
    from tranibor_orchestrator.queues import activity_name

    names = {activity_name(fn) for fn in all_activities()}
    assert collect_run_artifacts in all_activities()
    assert not {"open_pull_request", "enqueue_merge", "ensure_branch", "check_scope_violations"} & names


def test_readiness_asks_no_repository_and_netops_proposes_through_the_core_s_forge() -> None:
    from tranibor_adapters import build
    from tranibor_adapters.fakes.gitops import FakeGitOps
    from tranibor_api.extensions import readiness_items
    from tranibor_api.services.readiness import need_a_repository

    assert readiness_items() == () and need_a_repository() == frozenset()
    nginx = build("web_server", "fake", {"repo": "acme/gitops"})
    assert isinstance(nginx.gitops.forge, FakeGitOps)
