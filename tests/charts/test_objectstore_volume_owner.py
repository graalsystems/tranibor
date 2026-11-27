# SPDX-License-Identifier: Apache-2.0
"""S87-13: the embedded object store owns its volume on a block device.

On AKS (Azure Disk) a fresh volume is root's; RustFS runs as its image's user (10001) and stopped at
start with `Permission denied` until the pod named an `fsGroup`. k3s's `local-path` hands out
world-writable directories, which is why the single-node proof never saw it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"


def _objectstore_pod() -> dict:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "tranibor",
            str(CHART),
            "-n",
            "tranibor",
            "-f",
            str(CHART / "values" / "all-in-one.yaml"),
            "--set",
            "global.bootstrap.org=proof",
            "--set",
            "global.bootstrap.admins=admin@proof.example",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "tranibor-objectstore":
            return doc["spec"]["template"]["spec"]
    raise AssertionError("the all-in-one profile renders no tranibor-objectstore Deployment")


def test_the_object_store_pod_owns_its_volume_as_the_images_user() -> None:
    context = _objectstore_pod().get("securityContext") or {}
    assert context.get("fsGroup") == 10001, context
    assert context.get("runAsUser") == 10001, context
    assert context.get("runAsNonRoot") is True, context
