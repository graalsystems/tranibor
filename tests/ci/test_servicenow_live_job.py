"""The nightly proof on a real ServiceNow instance counts only green nights, and says why it skipped (S80-50).

Plan P1 asks for fourteen green nights of ServiceNow (S80-20). The nightly's `servicenow-live` job runs
the plugin's own live suite and the core's plugin tracker through the published server image. Ways it could
stop being a proof without turning red: it leaves the schedule or the alert's `needs`, the server image or the
plugin's source floats, a skip swallows a broken instance (a wrong password read as a sleeping one), or the
skip says nothing. The gate is run here as bash, with a stub `curl`, on each answer an instance can give.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
from typing import Any

import pytest
import yaml
from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"
JOB = "servicenow-live"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
SECRETS = ("URL", "USERNAME", "PASSWORD")
RAN = "steps.gate.outputs.skip == ''"


def _workflow() -> dict[str, Any]:
    return dict(yaml.safe_load(NIGHTLY.read_text(encoding="utf-8")))


def _job() -> dict[str, Any]:
    return dict(_workflow()["jobs"][JOB])


def _gate() -> dict[str, Any]:
    return next(step for step in _job()["steps"] if step.get("id") == "gate")


def test_the_job_is_in_the_night_and_its_failure_reaches_the_alert() -> None:
    jobs = _workflow()["jobs"]
    assert JOB in jobs["alerte"]["needs"], "the night's alert must wait for it"
    assert _job().get("continue-on-error", False) is False
    upstream = PUBLISHING.source_repository.removeprefix("https://github.com/")
    assert str(_job()["if"]) == f"github.repository == '{upstream}'", "forks have no secrets"


def test_the_schedule_runs_every_job_and_a_dispatch_can_run_this_one_alone() -> None:
    workflow = _workflow()
    raw: Any = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    trigger = raw.get("on", raw.get(True))  # YAML 1.1 reads a bare `on` as true
    assert JOB in trigger["workflow_dispatch"]["inputs"]["job"]["options"]
    assert trigger["workflow_dispatch"]["inputs"]["job"]["default"] == "all"
    for name, job in workflow["jobs"].items():
        if name in {JOB, "alerte"}:
            continue
        alone = f"{name} would run on a dispatch of {JOB} alone"
        assert f"inputs.job != '{JOB}'" in str(job.get("if", "")), alone


def test_the_credentials_are_secrets_and_past_the_gate_nothing_skips() -> None:
    env = _job()["env"]
    for name in SECRETS:
        variable = f"TRANIBOR_LIVE_SERVICENOW_{name}"
        assert str(env[variable]).replace(" ", "") == f"${{{{secrets.{variable}}}}}", variable
    assert env["TRANIBOR_LIVE_REQUIRED"] == "servicenow", "past the gate, a missing variable fails"


def test_the_server_image_and_the_plugin_source_are_pinned() -> None:
    env = _job()["env"]
    image = str(env["SERVICENOW_IMAGE"])
    assert image.startswith(f"{PUBLISHING.registry}/tranibor-agents/servicenow-mcp:"), image
    assert DIGEST.search(image), "the published digest"
    assert "sha256:" + "0" * 64 not in image, "never the placeholder"
    assert re.fullmatch(r"[0-9a-f]{40}", str(env["AGENTS_REF"])), "the plugin's suite at a commit"
    agents = [s for s in _job()["steps"] if s.get("with", {}).get("path") == "agents"]
    assert len(agents) == 1
    assert agents[0]["with"]["repository"] == PUBLISHING.plugin_repository.removeprefix("https://github.com/")
    assert agents[0]["with"]["ref"] == "${{ env.AGENTS_REF }}"


def test_both_suites_file_in_a_group_no_deployment_watches() -> None:
    """The agents repository's #66: a deployment's triage project watches the instance's Service Desk; it
    opened the suites' incidents as work items and reopened one a second after the suite resolved it."""
    group = str(_job()["env"]["TRANIBOR_LIVE_SERVICENOW_GROUP"])
    assert group and group != "Service Desk", group
    runs = [str(s.get("run", "")) for s in _job()["steps"]]
    served = [r for r in runs if "$SERVICENOW_IMAGE" in r]
    assert served and 'SERVICENOW_ASSIGNMENT_GROUPS="$TRANIBOR_LIVE_SERVICENOW_GROUP"' in served[0]


def test_both_suites_run_and_every_step_past_the_gate_waits_for_it() -> None:
    steps = _job()["steps"]
    assert steps[0].get("id") == "gate", "the gate decides before anything is checked out"
    for step in steps[1:]:
        named = step.get("name") or step.get("uses")
        assert RAN in str(step.get("if", "")), f"runs on a skipped night: {named}"
    runs = [(str(s.get("working-directory", "")), str(s.get("run", ""))) for s in steps]
    assert any(d == "agents/plugins/servicenow/server" and "pytest tests/live" in r for d, r in runs)
    assert any("tests/live/test_servicenow_live.py" in r and "-m live" in r for _, r in runs)
    assert any("$SERVICENOW_IMAGE" in r for _, r in runs), "the core's suite goes through the published image"


def _run_gate(tmp_path: pathlib.Path, answer: str | None, **env: str) -> tuple[int, str, str]:
    """The gate's own script, with a stub `curl` that prints `answer` as the `-w` line (or fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    if answer is None:
        curl.write_text("#!/bin/sh\necho 'curl: (6) Could not resolve host' >&2\nexit 6\n", encoding="utf-8")
    else:
        curl.write_text(f"#!/bin/sh\nprintf '%s' '{answer}'\n", encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    output, summary = tmp_path / "output", tmp_path / "summary"
    full = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        **{f"TRANIBOR_LIVE_SERVICENOW_{n}": "x" for n in SECRETS},
        **env,
    }
    done = subprocess.run(  # the workflow's own script, run as the runner runs it
        ["bash", "-e", "-c", str(_gate()["run"])],
        cwd=tmp_path,
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    told = output.read_text(encoding="utf-8") if output.exists() else ""
    said = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return done.returncode, told, said + done.stdout


@pytest.mark.parametrize("missing", SECRETS)
def test_a_missing_secret_skips_and_names_the_reason(tmp_path: pathlib.Path, missing: str) -> None:
    unset = {f"TRANIBOR_LIVE_SERVICENOW_{missing}": ""}
    code, told, said = _run_gate(tmp_path, "200 application/json", **unset)
    assert (code, told) == (0, "skip=secrets-absent\n")
    assert "skipped (`secrets-absent`)" in said


@pytest.mark.parametrize("answer", ["302 text/html", "200 text/html;charset=UTF-8", "307 "])
def test_a_sleeping_instance_skips_and_names_the_reason(tmp_path: pathlib.Path, answer: str) -> None:
    code, told, said = _run_gate(tmp_path, answer)
    assert (code, told) == (0, "skip=instance-hibernating\n")
    assert "skipped (`instance-hibernating`)" in said


def test_an_awake_instance_runs_the_suites(tmp_path: pathlib.Path) -> None:
    assert _run_gate(tmp_path, "200 application/json;charset=UTF-8")[:2] == (0, "skip=\n")


@pytest.mark.parametrize(
    "answer", ["401 application/json;charset=UTF-8", "403 application/json", "500 text/plain", None]
)
def test_anything_else_is_a_red_night_not_a_skip(tmp_path: pathlib.Path, answer: str | None) -> None:
    code, told, _ = _run_gate(tmp_path, answer)
    assert code != 0, f"{answer!r} must turn the night red"
    assert "skip=" not in told
