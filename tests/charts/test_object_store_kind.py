"""S87-17: `global.objectStore.kind` renders the store the API and the workers write the files to, and
what its identity needs.

Read from the render, as `helm template` produces it: `CHOREGOS_OBJECT_STORE_KIND` and the credentials
each kind reads, the Azure Workload Identity label on the pods that hold no key, and the guards that
refuse a kind its URL, endpoint, Secret or backup does not go with. No cloud ran this.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"
AZURE = [
    "--set",
    "global.objectStore.kind=azure-blob",
    "--set",
    "global.objectStore.url=azblob://files/choregos",
    "--set",
    "global.objectStore.endpoint=https://acme.blob.core.windows.net",
]
WORKLOAD_LABEL = "azure.workload.identity/use"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "choregos", str(CHART), "-n", "choregos", *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _platform_pods(*arguments: str) -> list[dict[str, Any]]:
    """The API's and the workers' Deployments: the processes that read the object store."""
    documents = [d for d in yaml.safe_load_all(_render(*arguments).stdout) if d]
    pods = [
        d
        for d in documents
        if d["kind"] == "Deployment"
        and (
            d["metadata"]["name"] == "choregos-api"
            or d["metadata"]["name"].startswith("choregos-orchestrator-")
        )
    ]
    assert len(pods) >= 2, [d["metadata"]["name"] for d in pods]
    return pods


def _env(deployment: dict[str, Any]) -> dict[str, Any]:
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in env}


def _refused(*arguments: str) -> str:
    result = _render(*arguments, check=False)
    assert result.returncode != 0, "rendered, and should have been refused"
    return result.stderr


def test_s3_is_the_default_and_keeps_its_keys() -> None:
    for pod in _platform_pods("--set", "global.objectStore.secretRef=their-s3"):
        env = _env(pod)
        assert env["CHOREGOS_OBJECT_STORE_KIND"] == "s3"
        assert env["CHOREGOS_OBJECT_STORE_ACCESS_KEY"]["secretKeyRef"] == {
            "name": "their-s3",
            "key": "access-key",
        }
        assert "CHOREGOS_OBJECT_STORE_ACCOUNT_KEY" not in env
        assert WORKLOAD_LABEL not in pod["spec"]["template"]["metadata"]["labels"]


def test_s3_without_a_secret_leaves_the_pod_to_its_aws_identity() -> None:
    annotation = r"eks\.amazonaws\.com/role-arn=arn:aws:iam::123456789012:role/tranibor"
    for pod in _platform_pods("--set-string", f"global.serviceAccount.annotations.{annotation}"):
        env = _env(pod)
        assert env["CHOREGOS_OBJECT_STORE_KIND"] == "s3"
        assert "CHOREGOS_OBJECT_STORE_ACCESS_KEY" not in env, "no key: the API takes the IRSA role"


def test_azure_blob_with_workload_identity_labels_the_pods() -> None:
    for pod in _platform_pods(*AZURE):
        env = _env(pod)
        assert env["CHOREGOS_OBJECT_STORE_KIND"] == "azure-blob"
        assert env["CHOREGOS_OBJECT_STORE_URL"] == "azblob://files/choregos"
        assert env["CHOREGOS_OBJECT_STORE_ENDPOINT"] == "https://acme.blob.core.windows.net"
        assert "CHOREGOS_OBJECT_STORE_ACCOUNT_KEY" not in env
        assert pod["spec"]["template"]["metadata"]["labels"][WORKLOAD_LABEL] == "true", pod["metadata"][
            "name"
        ]


def test_azure_blob_with_a_key_reads_it_and_needs_no_label() -> None:
    for pod in _platform_pods(*AZURE, "--set", "global.objectStore.secretRef=bench-blob"):
        env = _env(pod)
        assert env["CHOREGOS_OBJECT_STORE_ACCOUNT_KEY"]["secretKeyRef"] == {
            "name": "bench-blob",
            "key": "account-key",
            "optional": True,
        }
        assert env["CHOREGOS_OBJECT_STORE_SAS_TOKEN"]["secretKeyRef"]["key"] == "sas-token"
        assert "CHOREGOS_OBJECT_STORE_ACCESS_KEY" not in env, "S3 keys are not asked of an Azure Secret"
        assert WORKLOAD_LABEL not in pod["spec"]["template"]["metadata"]["labels"]


def test_gcs_renders_its_kind() -> None:
    for pod in _platform_pods(
        "--set", "global.objectStore.kind=gcs", "--set", "global.objectStore.url=gs://files"
    ):
        env = _env(pod)
        assert env["CHOREGOS_OBJECT_STORE_KIND"] == "gcs"
        assert env["CHOREGOS_OBJECT_STORE_URL"] == "gs://files"
        assert "CHOREGOS_OBJECT_STORE_ACCESS_KEY" not in env


def test_a_kind_its_settings_do_not_go_with_is_refused() -> None:
    assert "unknown" in _refused("--set", "global.objectStore.kind=minio")
    assert "starts with gs://" in _refused("--set", "global.objectStore.kind=gcs")
    assert "starts with azblob://" in _refused(
        "--set",
        "global.objectStore.kind=azure-blob",
        "--set",
        "global.objectStore.endpoint=https://a.blob.core.windows.net",
    )
    assert "needs global.objectStore.endpoint" in _refused(
        "--set", "global.objectStore.kind=azure-blob", "--set", "global.objectStore.url=azblob://files"
    )
    assert "takes no secretRef" in _refused(
        "--set",
        "global.objectStore.kind=gcs",
        "--set",
        "global.objectStore.url=gs://files",
        "--set",
        "global.objectStore.secretRef=hmac",
    )
    assert "goes with kind `s3`" in _refused(*AZURE, "--set", "global.objectStore.embedded=true")
    assert "global.objectStore.kind=s3" in _refused(
        *AZURE,
        "--set",
        "global.objectStore.secretRef=bench-blob",
        "--set",
        "backup.enabled=true",
        "--set",
        "backup.objectStore=true",
    )
    assert "already set by the chart" in _refused(
        "--set", "global.extraEnv[0].name=CHOREGOS_OBJECT_STORE_KIND", "--set", "global.extraEnv[0].value=gcs"
    )
