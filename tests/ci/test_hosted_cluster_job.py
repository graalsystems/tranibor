"""The CI job where a hosted agent's pod runs on a real Kubernetes (S41-24, ADR 0080).

Every test of `tests/cluster/` skips without a cluster, an image or the RuntimeClass, and a skipped
test is a green job: these checks keep the job building the images, running the tests on 1.34 and
on 1.28, failing on a skip, behind the gate.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, Any]:
    return dict(yaml.safe_load(CI.read_text(encoding="utf-8")))


def _job() -> dict[str, Any]:
    return dict(_workflow()["jobs"]["hosted-cluster"])


def _runs() -> str:
    return "\n".join(str(step.get("run", "")) for step in _job()["steps"])


def test_the_job_runs_the_hosted_tests_on_two_versions_and_fails_on_a_skip() -> None:
    runs = _runs()
    for test in ("test_hosted_native_sidecar.py", "test_hosted_session_pod.py", "test_hosted_gvisor.py"):
        assert f"tests/cluster/{test}" in runs, test
    assert "kindest/node:v1.28" in runs
    assert "TRANIBOR_CLUSTER_CONTEXT=kind-old" in runs
    assert "! grep -qi skipped pytest.out" in runs
    assert "make cluster-gvisor" in runs
    assert not any("if" in step for step in _job()["steps"]), "no step of the proof is skipped"


def test_the_images_are_built_and_loaded_into_the_cluster() -> None:
    runs = _runs()
    assert "examples/hosted/reference" in runs
    assert "tests/cluster/assets/runner.Dockerfile" in runs
    assert "kind load docker-image" in runs
    env = next(step["env"] for step in _job()["steps"] if "env" in step)
    assert env["TRANIBOR_HOSTED_REFERENCE_IMAGE"] == "reference-agent:ci"
    assert env["TRANIBOR_HOSTED_RUNNER_IMAGE"] == "choregos-runner:ci"


def test_the_gate_waits_for_the_job_and_the_filter_covers_what_it_proves() -> None:
    workflow = _workflow()
    assert "hosted-cluster" in workflow["jobs"]["ci-ok"]["needs"]
    assert "hosted-cluster" in workflow["jobs"]["changes"]["outputs"]
    filters = yaml.safe_load(workflow["jobs"]["changes"]["steps"][1]["with"]["filters"])["hosted-cluster"]
    for path in (
        "tests/cluster/**",
        "packages/adapters/src/tranibor_adapters/executor/k8s_job.py",
        "packages/runner/src/tranibor_runner/serve_session.py",
        "examples/hosted/reference/**",
    ):
        assert path in filters, path
    assert "github.ref == 'refs/heads/main'" in _job()["if"]


def test_the_job_runs_tektons_agent_stage_under_gvisor() -> None:
    """S87-06: Tekton Pipelines, the release the proof names and checked against its checksum, installed
    after gVisor, then the isolation probe and one agent run, both under the `gvisor` RuntimeClass, with
    the runner image this job builds; a skip fails the job."""
    steps = _job()["steps"]
    runs = _runs()
    [install] = [i for i, s in enumerate(steps) if "v1.6.0/release.yaml" in str(s.get("run"))]
    assert "tektoncd/pipeline/releases/download/v1.6.0/release.yaml" in steps[install]["run"]
    assert "sha256sum -c" in steps[install]["run"]
    assert install > next(i for i, s in enumerate(steps) if "make cluster-gvisor" in str(s.get("run")))
    probe = "tests/cluster/test_tekton_isolation.py"
    [tekton] = [i for i, s in enumerate(steps) if probe in str(s.get("run"))]
    assert tekton > install
    run = steps[tekton]["run"]
    assert "tests/cluster/test_tekton_agent_run.py" in run
    assert "TRANIBOR_TEKTON_RUNTIME_CLASS=gvisor" in run
    assert 'TRANIBOR_TEKTON_RUNNER_IMAGE="$TRANIBOR_HOSTED_RUNNER_IMAGE"' in run
    assert "TRANIBOR_CLUSTER_CONTEXT=$(kubectl config current-context)" in run
    assert "use-context kind-" in runs, "the current context is the 1.34 cluster again"
    assert "! grep -qE '^SKIPPED|[0-9]+ skipped' tekton.out" in run, "a skip fails the job"
    filters = yaml.safe_load(_workflow()["jobs"]["changes"]["steps"][1]["with"]["filters"])["hosted-cluster"]
    assert "packages/adapters/src/tranibor_adapters/executor/tekton.py" in filters
