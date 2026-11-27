"""`tools/cluster-proof/proof.sh` (S87-09) against scripted `kubectl`, `helm` and `uv`: no cluster.

The script's verdicts are proven on real clusters (the `cluster-proof` CI job on kind and k3d, the AKS
runs of S87-13). What is proven here is its shape, which a real cluster only shows when something
breaks: every step runs, including the object store's, and a failure at ANY step still removes the
release and its namespace, and still leaves the evidence and its archive behind.

The fakes write each call to a file and answer what the script parses; `PROOF_FAKE_FAIL` makes the
call whose arguments contain it exit 1.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tarfile
from typing import Any

import pytest
import yaml
from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROOF = ROOT / "tools" / "cluster-proof" / "proof.sh"

_PRELUDE = """#!/usr/bin/env bash
line="$*"
echo "$(basename "$0") ${line//$'\\n'/ }" >>"$PROOF_FAKE_CALLS"
if [ -n "${PROOF_FAKE_FAIL:-}" ]; then
  case "$*" in *"$PROOF_FAKE_FAIL"*) exit 1 ;; esac
fi
args="$*"
"""

KUBECTL = (
    _PRELUDE
    + """case "$args" in
  *" version -o json"*) echo '{"serverVersion": {"gitVersion": "v1.34.0"}}' ;;
  *"get deploy tranibor-api -o jsonpath"*) echo API_IMAGE ;;
  *"get deploy,statefulset -o name"*) echo deployment.apps/tranibor-api ;;
  *"get pod probe-"*)
    case "$args" in *postgresql* | *temporal* | *objectstore*) echo Failed ;; *) echo Succeeded ;; esac ;;
  *"get pod -l job-name="*"status.phase"*) echo Succeeded ;;
  *"logs job/proof-"*"-c runner"*) echo '{"first": "closed", "open": 0, "attempts": 9}' ;;
  *"logs job/proof-gated -c egress-gate"*) echo '{"outcome": "closed"}' ;;
  *"tokens create"*) echo a-token ;;
  *"python - run"*) cat >/dev/null; echo 01-work-item ;;
  *"python - check"*) cat >/dev/null; echo "work item 01-work-item: HTTP 200, state approved" ;;
  *"tranibor_api.object_store"*) echo "s3 at http://tranibor-objectstore:9000: s3://tranibor/proof written" ;;
  *"apply -f -"*) cat >/dev/null ;;
esac
exit 0
"""
).replace("API_IMAGE", f"{PUBLISHING.image_name('api')}:proof")

HELM = _PRELUDE + 'case "$args" in *" install tranibor "*) echo "STATUS: deployed" ;; esac\nexit 0\n'

UV = (
    _PRELUDE
    + """case "$args" in
  *pytest*) echo "pod: runtimeClass=None init={'agent': 0}"; echo "2 passed in 9.1s" ;;
  *"cluster check --context"*) echo "the cluster has what the chart needs" ;;
esac
exit 0
"""
)


def _run(tmp_path: pathlib.Path, fail: str = "", keep: bool = False) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"kubectl": KUBECTL, "helm": HELM, "uv": UV}.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PROOF_CONTEXT": "kind-proof",
        "PROOF_DOMAIN": "tranibor.proof.test",
        "PROOF_IMAGE_TAG": "proof",
        "PROOF_EVIDENCE": str(tmp_path / "evidence"),
        "PROOF_FAKE_CALLS": str(calls),
        "PROOF_FAKE_FAIL": fail,
    }
    if keep:
        env["PROOF_KEEP"] = "1"
    result = subprocess.run(
        ["bash", str(PROOF)], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    return result.returncode, result.stdout + result.stderr, calls.read_text().splitlines()


def _removed(calls: list[str]) -> bool:
    uninstalled = any(c.startswith("helm ") and " uninstall tranibor -n tranibor" in c for c in calls)
    deleted = any(c.startswith("kubectl ") and " delete namespace tranibor" in c for c in calls)
    return uninstalled and deleted


def _archived(tmp_path: pathlib.Path) -> set[str]:
    with tarfile.open(tmp_path / "evidence.tar.gz") as archive:
        return {pathlib.PurePath(name).name for name in archive.getnames()}


def test_every_step_runs_and_the_release_is_removed_after_a_proof_that_held(tmp_path: pathlib.Path) -> None:
    status, output, calls = _run(tmp_path)

    assert status == 0, output
    steps = (tmp_path / "evidence" / "steps.log").read_text()
    for step in (
        "preflight",
        "install the chart",
        "an agent pod reaches what a run uses",
        "the site's object store",
        "the egress gate",
        "under the deployment's RuntimeClass",
        "one work item to completion",
        "PROOF HELD",
    ):
        assert step in steps, step
    # The object store: walled off from an agent pod, and used by the API with its own settings.
    assert any(" run probe-tranibor-objectstore-9000 " in c for c in calls)
    assert "agent pod -> tranibor-objectstore:9000: closed (expected closed)" in steps
    assert any(
        "exec deploy/tranibor-api -- python -c" in c and "tranibor_api.object_store" in c for c in calls
    )
    # The platform's images of this commit, by tag.
    assert any(" install tranibor " in c and "global.imageTag=proof" in c for c in calls)
    assert _removed(calls)
    assert {"steps.log", "helm-install.txt", "isolation.txt", "pods.txt", "events.txt"} <= _archived(tmp_path)


# One failure per step, each where the step's own command fails.
FAILURES = {
    "preflight": "cluster check --context",
    "install": " install tranibor ",
    "rollout": "rollout status",
    "object store": "tranibor_api.object_store",
    "egress gate": "logs job/proof-gated -c runner",
    "isolation probe": "test_agent_isolation.py",
    "token": "tokens create",
    "work item": "python - check",
}


@pytest.mark.parametrize("fail", FAILURES.values(), ids=FAILURES.keys())
def test_a_failure_at_any_step_still_removes_the_release_and_keeps_the_evidence(
    tmp_path: pathlib.Path, fail: str
) -> None:
    status, output, calls = _run(tmp_path, fail=fail)

    assert status != 0, output
    steps = (tmp_path / "evidence" / "steps.log").read_text()
    assert "PROOF FAILED" in steps
    assert "PROOF HELD" not in steps
    assert _removed(calls), calls[-5:]
    # What the pods said is part of the evidence of a failure, and the archive holds it all.
    assert (tmp_path / "evidence" / "pods-describe.txt").exists()
    assert {"steps.log", "pods.txt", "events.txt", "pods-describe.txt"} <= _archived(tmp_path)


def test_proof_keep_leaves_the_release_for_a_person_to_read(tmp_path: pathlib.Path) -> None:
    status, _, calls = _run(tmp_path, fail="tokens create", keep=True)

    assert status != 0
    assert not any(" uninstall " in c or " delete namespace " in c for c in calls)
    assert "steps.log" in _archived(tmp_path)


def _workloads(*values: str) -> dict[str, dict[str, Any]]:
    """The rendered pods' containers by `<workload>/<container>`: requests and limits."""
    arguments = ["helm", "template", "tranibor", str(ROOT / "charts" / "tranibor"), "-n", "tranibor"]
    for file in values:
        arguments += ["-f", str(ROOT / file)]
    arguments += [
        "--set",
        "global.bootstrap.org=proof",
        "--set",
        "global.bootstrap.admins=admin@proof.example",
    ]
    rendered = subprocess.run(arguments, check=True, capture_output=True, text=True).stdout
    found: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(rendered):
        if doc and doc["kind"] in {"Deployment", "StatefulSet"}:
            for container in doc["spec"]["template"]["spec"]["containers"]:
                found[f"{doc['metadata']['name']}/{container['name']}"] = container.get("resources", {})
    return found


def _millicores(value: str) -> int:
    return int(value[:-1]) if value.endswith("m") else int(float(value) * 1000)


def test_the_small_node_overlay_lowers_the_platform_cpu_requests_and_nothing_else() -> None:
    profile = _workloads("charts/tranibor/values/all-in-one.yaml")
    small = _workloads("charts/tranibor/values/all-in-one.yaml", "tools/cluster-proof/small-node.yaml")

    assert small.keys() == profile.keys()
    requested = sum(_millicores(r["requests"]["cpu"]) for r in small.values() if "requests" in r)
    # With kind's or k3s's own 1.1 CPU, a 4 vCPU node keeps one full CPU for an agent pod.
    assert requested <= 1700, requested
    assert requested < sum(_millicores(r["requests"]["cpu"]) for r in profile.values() if "requests" in r)
    for name, resources in small.items():
        assert resources.get("limits") == profile[name].get("limits"), name
        memory = resources.get("requests", {}).get("memory")
        assert memory == profile[name].get("requests", {}).get("memory"), name
