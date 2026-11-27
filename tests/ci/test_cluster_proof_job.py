"""The CI job that runs the install proof on kind and on k3d (S87-09).

`tools/cluster-proof/proof.sh` exits 0 only when every step held; these checks keep the job running it
on both clusters with the images of this commit, keeping the evidence and deleting the cluster whatever
the outcome, behind the gate.
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
    return dict(_workflow()["jobs"]["cluster-proof"])


def _step(fragment: str) -> dict[str, Any]:
    return next(step for step in _job()["steps"] if fragment in str(step.get("run", step.get("uses", ""))))


def test_the_proof_runs_on_kind_and_on_k3d_with_the_images_of_this_commit() -> None:
    job = _job()
    assert job["strategy"]["matrix"]["cluster"] == ["kind", "k3d"]
    assert job["strategy"]["fail-fast"] is False
    assert 'tools/cluster-proof/cluster.sh up "$CLUSTER"' in _step("cluster.sh up")["run"]
    assert "proof.sh tools/cluster-proof/small-node.yaml" in _step("tools/cluster-proof/proof.sh")["run"]
    assert "if" not in _step("tools/cluster-proof/proof.sh"), "the proof itself is never skipped"
    build = _step("docker build")["run"]
    for target in ("--target api", "--target worker", "docker/web.Dockerfile"):
        assert target in build, target
    assert job["env"]["PROOF_IMAGE_TAG"] == "proof"
    assert set(job["env"]["PROOF_LOAD_IMAGES"].split()) == {
        f"ghcr.io/graalsystems/tranibor-{name}:proof" for name in ("api", "orchestrator", "web")
    }
    # What it pulls is pinned: both node images by digest, k3d by checksum.
    assert "@sha256:" in job["env"]["KIND_NODE_IMAGE"]
    assert "@sha256:" in job["env"]["K3S_NODE_IMAGE"]
    assert "sha256sum -c" in _step("k3d-io/k3d")["run"]


def test_the_evidence_is_kept_and_the_cluster_deleted_whatever_the_outcome() -> None:
    upload = _step("actions/upload-artifact")
    assert upload["if"] == "always()"
    assert upload["with"]["path"].endswith(".tar.gz")
    down = _step("cluster.sh down")
    assert down["if"] == "always()"
    assert _job()["steps"].index(upload) < _job()["steps"].index(down)


def test_the_gate_waits_for_the_job_and_the_filter_covers_what_it_proves() -> None:
    workflow = _workflow()
    assert "cluster-proof" in workflow["jobs"]["ci-ok"]["needs"]
    assert "cluster-proof" in workflow["jobs"]["changes"]["outputs"]
    filters = yaml.safe_load(workflow["jobs"]["changes"]["steps"][1]["with"]["filters"])["cluster-proof"]
    for path in ("tools/cluster-proof/**", "charts/**", "docker/**", "tests/cluster/test_agent_isolation.py"):
        assert path in filters, path
    assert "github.ref == 'refs/heads/main'" in _job()["if"]
    assert _job()["timeout-minutes"] <= 60
