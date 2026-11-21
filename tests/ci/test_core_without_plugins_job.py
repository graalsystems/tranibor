"""The core's suite runs with the first-party plugins' extensions uninstalled (ADR 0181, round D).

Two jobs carry the proof: `core-without-plugins` in `ci.yml` runs the boundary on every Python PR, and the
nightly job of the same name runs the core's whole suite, less the files that still need a plugin
(`tests/core_alone/need_the_plugins.txt`). Both build the environment without the two extensions and set
`TRANIBOR_TEST_CORE_ALONE=1`, which turns on `tests/core_alone`: a job that installed them by mistake fails
there instead of proving nothing.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEFT_OUT = ("tranibor-software-delivery", "tranibor-investment-research")


def _job(workflow: str) -> dict[str, Any]:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))["jobs"]
    return dict(jobs["core-without-plugins"])


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in job["steps"] if "run" in step]


@pytest.mark.parametrize("workflow", ["ci.yml", "nightly.yml"])
def test_the_job_builds_the_environment_without_the_plugins_extensions(workflow: str) -> None:
    job = _job(workflow)
    [sync] = [run for run in _runs(job) if run.startswith("uv sync")]
    for distribution in LEFT_OUT:
        assert f"--no-install-package {distribution}" in sync, (workflow, distribution)
    assert job["env"]["TRANIBOR_TEST_CORE_ALONE"] == "1"
    tests = [run for run in _runs(job) if "pytest" in run]
    # `--no-sync`: a plain `uv run` would sync the workspace back, extensions included.
    assert tests and all("uv run --no-sync pytest" in run for run in tests), workflow


def test_the_pr_job_runs_the_core_alone_checks_and_the_seams() -> None:
    [tests] = [run for run in _runs(_job("ci.yml")) if "pytest" in run]
    for path in ("tests/core_alone", "tests/architecture", "tests/replay", "test_worker_seam.py"):
        assert path in tests, path


def test_the_pr_job_is_part_of_the_gate() -> None:
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]
    assert "core-without-plugins" in jobs["ci-ok"]["needs"]


def test_the_nightly_job_runs_the_whole_core_suite_less_the_listed_files() -> None:
    job = _job("nightly.yml")
    [tests] = [run for run in _runs(job) if "pytest" in run]
    assert "tests/core_alone/need_the_plugins.txt" in tests
    assert "packages apps tests" in tests and "plugins" not in tests.replace("need_the_plugins", "")
    nightly = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8"))
    assert "core-without-plugins" in nightly["jobs"]["alerte"]["needs"]
