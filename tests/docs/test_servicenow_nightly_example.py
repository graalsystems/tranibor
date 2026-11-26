# SPDX-License-Identifier: Apache-2.0
"""S80-46: the ServiceNow nightly's workflow stays one the platform publishes and a runner can execute.

The file in `examples/servicenow-nightly/` is the source of the workflow a deployment runs every night
(`docs/runbooks/servicenow-nightly.md`). Edited here and refused by the platform, or by the runner, the next
night would be red for a reason that is not the instance's.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml
from tranibor_contracts import JobActor
from tranibor_core import parse_workflow

WORKFLOW = Path(__file__).resolve().parents[2] / "examples" / "servicenow-nightly" / "workflow.yaml"
#: What the job may reach: reads, and the one write the nightly project's group is allowed.
TOOLS = {"search_records", "tracker_create_item", "tracker_fetch_item"}


def _probe() -> JobActor:
    workflow, report = parse_workflow(WORKFLOW.read_text(encoding="utf-8"), strict=False)
    assert report.valid, " · ".join(i.format() for i in report.errors)
    actor = workflow.actors["probe"]
    assert isinstance(actor, JobActor)
    return actor


def test_the_nightly_is_a_valid_workflow_whose_only_step_is_a_job() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert [t["by"] for t in document["transitions"]] == ["probe"]
    assert {name for name, state in document["states"].items() if state.get("terminal")} == {"green", "red"}
    minutes = _probe().max_minutes
    assert minutes is not None and minutes > 30  # the probe reads the incident back for 30 minutes at most


def test_the_job_runs_in_the_runner_image_with_python_alone() -> None:
    probe = _probe()
    assert probe.image is None, "the runner image: no job image to map on the deployment"
    assert probe.command[:2] == ["python3", "-c"]
    tree = ast.parse(probe.command[2])
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    imported = {alias.name.split(".")[0] for node in imports for alias in node.names}
    assert imported <= {"json", "os", "time", "urllib"}, imported


def test_the_job_reaches_the_instance_only_through_the_connector() -> None:
    probe = _probe()
    assert [(t.connector, set(t.tools)) for t in probe.tools] == [("servicenow", TOOLS)]
    script = probe.command[2]
    assert "/tools/servicenow__" in script and "TRANIBOR_RUN_TOKEN" in script
    assert "service-now.com" not in script and "password" not in script.lower()


def test_the_job_always_leaves_a_verdict() -> None:
    script = _probe().command[2]
    assert ".tranibor/job.json" in script
    assert script.count("GREEN:") == 1 and script.count("RED:") >= 4
