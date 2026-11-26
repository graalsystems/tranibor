"""The default Secret names are stored state and keep their names (ADR 0226 §3, §6).

A Secret the chart reads by a default name is one a deployment provided under that name (a sealed
secret, a vault operator, `kubectl create secret`); a Secret the chart draws is re-read under the name
it was written with. Renaming a default breaks both on upgrade, and under a renderer that cannot read
the cluster (Argo CD) the second would be drawn again, `secrets-keks` included, leaving every stored
connector secret unreadable. So every Secret name the chart reads or draws, with no values and with
each profile, is pinned here to what it was before ADR 0226 (7bf2576a6). A deployment that wants other
names sets them in its values, after copying each Secret (docs/deployment.md).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
OPERATOR = ["--api-versions", "extensions.agents.x-k8s.io/v1beta1/SandboxTemplate"]
SITE = ["--set", "global.bootstrap.org=acme", "--set", "global.bootstrap.admins=root@acme.example"]
VALUES = CHART / "values"

#: The release is `platform`: the names that start with it are the release's, the others the chart's.
PINNED: dict[str, tuple[list[str], set[str]]] = {
    "default": (
        [],
        {
            "choregos-api-secrets",
            "choregos-db",
            "choregos-gateway-secrets",
            "choregos-oidc",
            "platform-temporal-codec",
        },
    ),
    "all-in-one": (
        ["-f", str(VALUES / "all-in-one.yaml"), *SITE],
        {
            "choregos-api-secrets",
            "choregos-object-store",
            "choregos-oidc",
            "platform-gateway",
            "platform-keycloak",
            "platform-memory",
            "platform-postgresql",
            "platform-temporal-codec",
        },
    ),
    "single-node": (
        ["-f", str(VALUES / "single-node.yaml"), *SITE, "--set", "backup.target.s3.url=s3://b/p"],
        {
            "choregos-api-secrets",
            "choregos-backup-target",
            "choregos-oidc",
            "platform-gateway",
            "platform-memory",
            "platform-postgresql",
            "platform-temporal-codec",
        },
    ),
    **{
        environment: (
            ["-f", str(VALUES / f"{environment}.yaml")],
            {
                "choregos-api-secrets",
                "choregos-db",
                "choregos-gateway-secrets",
                "choregos-oidc",
                "platform-temporal-codec",
            },
        )
        for environment in ("dev", "staging", "prod")
    },
    "netops": (
        ["--set", "netops.ciliumReader.enabled=true"],
        {
            "choregos-api-secrets",
            "choregos-db",
            "choregos-gateway-secrets",
            "choregos-oidc",
            "choregos-netops-cilium-reader-token",
            "platform-temporal-codec",
        },
    ),
}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _secret_names(node: Any, found: set[str]) -> None:
    """Every Secret a document names: rendered, targeted by an ExternalSecret, or read by a pod."""
    if isinstance(node, dict):
        if node.get("kind") == "Secret" and "metadata" in node:
            found.add(node["metadata"]["name"])
        if node.get("kind") == "ExternalSecret":
            found.add(node["spec"]["target"]["name"])
        for key, value in node.items():
            if key in ("secretKeyRef", "secretRef") and isinstance(value, dict) and "name" in value:
                found.add(value["name"])
            elif key == "secret" and isinstance(value, dict):
                found.add(value.get("secretName") or value["name"])
            _secret_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _secret_names(item, found)


@pytest.mark.parametrize("profile", sorted(PINNED))
def test_every_secret_name_the_chart_reads_or_draws_is_the_one_before_the_rename(profile: str) -> None:
    arguments, pinned = PINNED[profile]
    command = ["helm", "template", "platform", str(CHART), "-n", "platform", *OPERATOR, *arguments]
    rendered = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
    assert rendered.returncode == 0, rendered.stderr
    found: set[str] = set()
    for document in yaml.safe_load_all(rendered.stdout):
        _secret_names(document, found)
    assert found == pinned


def test_the_netops_reader_keeps_its_service_account() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["netops"]["ciliumReader"]["name"] == "choregos-netops-cilium-reader"
