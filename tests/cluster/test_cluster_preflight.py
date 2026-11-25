"""S87-03 — `tranibor cluster check` on a real cluster: what the CNI does, not what the API accepts.

The check is run as an operator runs it, against the kube context the suite names. Which outcome is
right depends on the cluster's CNI, read from `kube-system`:

- kind's default `kindnet` accepts a NetworkPolicy and lets the traffic through: the enforcement check
  must FAIL and say so (the kind cluster `dev/kind.yaml` describes turns kindnet off for that reason);
- Calico (`make cluster-up`) enforces it: the check must pass.

A cluster with neither is skipped rather than judged: the test would only be guessing.

    kind create cluster --name preflight-kindnet                  # kindnet
    TRANIBOR_CLUSTER_CONTEXT=kind-preflight-kindnet \
      uv run pytest tests/cluster/test_cluster_preflight.py -m cluster
    make cluster-up                                               # Calico
    TRANIBOR_CLUSTER_CONTEXT=kind-choregos \
      uv run pytest tests/cluster/test_cluster_preflight.py -m cluster

`TRANIBOR_CLUSTER_PROBE_IMAGE` names a mirror of the probe image where the nodes cannot pull
`registry.k8s.io`. The test creates nothing itself: the check makes one namespace and deletes it.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from typer.testing import CliRunner

from .conftest import context, kubectl_shared

pytestmark = pytest.mark.cluster


def _cni() -> str:
    daemonsets = kubectl_shared("-n", "kube-system", "get", "daemonsets", "-o", "name").split()
    names = {name.removeprefix("daemonset.apps/") for name in daemonsets}
    if "calico-node" in names:
        return "calico"
    if "kindnet" in names:
        return "kindnet"
    return ""


def _check() -> dict[str, Any]:
    from tranibor_cli.main import app

    arguments = [
        "cluster",
        "check",
        "--context",
        context(non_destructive=True),
        "--executor",
        "k8s_job",
        "--no-monitoring",
        # Reachable from any pod under no policy: the object store probe has something to find.
        "--object-store-endpoint",
        "https://kubernetes.default.svc:443",
        "--json",
    ]
    if image := os.environ.get("TRANIBOR_CLUSTER_PROBE_IMAGE", ""):
        arguments += ["--probe-image", image]
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code in {0, 1}, result.output
    report: dict[str, Any] = json.loads(result.output)
    report["exit_code"] = result.exit_code
    return report


def test_the_preflight_judges_the_cni_by_what_it_enforces() -> None:
    cni = _cni()
    if not cni:
        pytest.skip("neither kindnet nor Calico in kube-system: the expected outcome is not known")
    report = _check()
    checks = {check["id"]: check for check in report["checks"]}
    print(json.dumps(report, indent=2, ensure_ascii=False))  # the record of the run, under `-s`

    assert checks["kubernetes-version"]["status"] == "ok"
    assert checks["default-storage-class"]["status"] == "ok"
    assert checks["object-store"]["status"] == "ok"
    enforced = checks["network-policy-enforced"]
    if cni == "kindnet":
        assert enforced["status"] == "fail", enforced
        assert "NetworkPolicy is not enforced" in enforced["detail"]
        assert "Calico" in enforced["fix"]
        # With a way out, the gate stays open to its deadline; without one, it proves nothing.
        assert checks["egress-gate"]["status"] in {"fail", "not applicable"}
        assert report["exit_code"] == 1
    else:
        assert enforced["status"] == "ok", enforced
        assert checks["egress-gate"]["status"] in {"ok", "not applicable"}
        assert report["ok"] is True and report["exit_code"] == 0

    leftovers = json.loads(
        kubectl_shared(
            "get", "namespaces", "-l", "app.kubernetes.io/managed-by=tranibor-cluster-check", "-o", "json"
        )
    )
    assert all(item["metadata"].get("deletionTimestamp") for item in leftovers["items"]), (
        "a namespace was left"
    )
