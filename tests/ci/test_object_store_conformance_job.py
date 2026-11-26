"""S87-17: the CI runs the object store conformance suite against the real servers.

Without `TRANIBOR_TEST_OBJECT_STORE_DOCKER=1` the Docker tests skip, and a skipped test is a green job:
this keeps the step asking for them (with it, a missing daemon fails the step).
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_the_conformance_job_asks_for_the_real_object_stores() -> None:
    workflow: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["conformance-backends"]
    steps = [s for s in job["steps"] if "tests/conformance/object_stores" in str(s.get("run", ""))]
    assert len(steps) == 1, "no step runs tests/conformance/object_stores"
    assert steps[0]["env"]["TRANIBOR_TEST_OBJECT_STORE_DOCKER"] == "1"
    assert "-m conformance" in steps[0]["run"]
    assert "conformance-backends" in workflow["jobs"]["ci-ok"]["needs"]
