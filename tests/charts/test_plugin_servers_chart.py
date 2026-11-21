"""`global.plugins.servers` (ADR 0096, S43-06): the API's Role in the plugins namespace only, its
settings, the namespace when asked, and the platform's egress towards the servers.

Off by default: no Role, no variable, the API refuses a plugin that brings a server.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml
from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "choregos"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is missing")

ON: dict[str, Any] = {
    "global": {
        "plugins": {
            "servers": {"enabled": True, "registries": [PUBLISHING.image("tranibor-agents")]},
        },
        "runner": {"runtimeClass": "gvisor"},
    }
}


def _render(values: dict[str, Any], tmp_path: pathlib.Path) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    command = ["helm", "template", "choregos", str(CHART), "-n", "choregos", "-f", str(path)]
    return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)


def _docs(values: dict[str, Any], tmp_path: pathlib.Path) -> list[dict[str, Any]]:
    result = _render(values, tmp_path)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _api_env(docs: list[dict[str, Any]]) -> dict[str, str]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "choregos-api":
            env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
            return {e["name"]: e.get("value", "") for e in env}
    raise AssertionError("no choregos-api Deployment")


def _named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next((d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name), None)


def test_off_by_default(tmp_path: pathlib.Path) -> None:
    docs = _docs({}, tmp_path)
    assert _named(docs, "Role", "choregos-api-plugin-servers") is None
    assert not [n for n in _api_env(docs) if n.startswith("CHOREGOS_PLUGIN_SERVERS")]


def test_on_the_api_runs_servers_in_their_namespace_only(tmp_path: pathlib.Path) -> None:
    docs = _docs(ON, tmp_path)
    env = _api_env(docs)
    assert env["CHOREGOS_PLUGIN_SERVERS"] == "kubernetes"
    assert env["CHOREGOS_PLUGIN_SERVERS_NAMESPACE"] == "choregos-plugins"
    assert yaml.safe_load(env["CHOREGOS_PLUGIN_SERVERS_REGISTRIES"]) == [PUBLISHING.image("tranibor-agents")]
    assert env["CHOREGOS_PLUGIN_SERVERS_RUNTIME_CLASS"] == "gvisor", "the runner's RuntimeClass by default"
    assert env["CHOREGOS_PLUGIN_SERVERS_API_NAMESPACE"] == "choregos"
    role = _named(docs, "Role", "choregos-api-plugin-servers")
    assert role is not None and role["metadata"]["namespace"] == "choregos-plugins"
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert resources == {"secrets", "services", "deployments", "networkpolicies"}
    assert not [
        d
        for d in docs
        if d.get("kind") in {"ClusterRole", "ClusterRoleBinding"} and "plugin" in d["metadata"]["name"]
    ]
    binding = _named(docs, "RoleBinding", "choregos-api-plugin-servers")
    assert binding is not None
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "choregos-api", "namespace": "choregos"}
    ]
    assert _named(docs, "Namespace", "choregos-plugins") is None, "the namespace only when asked"
    platform = _named(docs, "NetworkPolicy", "choregos-allow-platform")
    assert platform is not None
    targets = [t for rule in platform["spec"]["egress"] for t in rule["to"]]
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "choregos-plugins"}}
    } in targets


def test_the_namespace_and_cilium_when_asked(tmp_path: pathlib.Path) -> None:
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["plugins"]["servers"].update(
        namespace={"name": "plugins", "create": True}, egress="cilium"
    )
    docs = _docs(values, tmp_path)
    namespace = _named(docs, "Namespace", "plugins")
    assert namespace is not None
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    role = _named(docs, "Role", "choregos-api-plugin-servers")
    assert role is not None and "ciliumnetworkpolicies" in {
        r for rule in role["rules"] for r in rule["resources"]
    }


@pytest.mark.parametrize(
    "servers",
    [{"namespace": {"name": "choregos"}}, {"egress": "calico"}],
    ids=["release-namespace", "unknown-egress"],
)
def test_refused(tmp_path: pathlib.Path, servers: dict[str, Any]) -> None:
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["plugins"]["servers"].update(servers)
    assert _render(values, tmp_path).returncode != 0


def test_rbac_false_leaves_the_role_to_the_platform(tmp_path: pathlib.Path) -> None:
    """S80-20: a platform that lays the Role down itself (as `rbac.jobRunner: false`) gets none from
    the chart, and the API still runs servers there."""
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["plugins"]["servers"].update(rbac=False, egress="cilium")
    docs = _docs(values, tmp_path)
    assert _named(docs, "Role", "choregos-api-plugin-servers") is None
    assert _named(docs, "RoleBinding", "choregos-api-plugin-servers") is None
    assert _api_env(docs)["CHOREGOS_PLUGIN_SERVERS"] == "kubernetes"


def test_the_pull_goes_through_the_mirror_with_its_secrets(tmp_path: pathlib.Path) -> None:
    """S80-20: the mirrors and the pull secrets reach the API; the secrets default to the release's."""
    mirror = {PUBLISHING.image("tranibor-agents"): "harbor.example/ghcr-proxy/tranibor-agents"}
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["imagePullSecrets"] = ["harbor-pull"]
    values["global"]["plugins"]["servers"]["imageMirrors"] = mirror
    env = _api_env(_docs(values, tmp_path))
    assert json.loads(env["CHOREGOS_PLUGIN_SERVERS_IMAGE_MIRRORS"]) == mirror
    assert json.loads(env["CHOREGOS_PLUGIN_SERVERS_IMAGE_PULL_SECRETS"]) == ["harbor-pull"]
    values["global"]["plugins"]["servers"]["imagePullSecrets"] = []
    assert "CHOREGOS_PLUGIN_SERVERS_IMAGE_PULL_SECRETS" not in _api_env(_docs(values, tmp_path))
    values["global"]["plugins"]["servers"]["imagePullSecrets"] = ["mirror-pull"]
    env = _api_env(_docs(values, tmp_path))
    assert json.loads(env["CHOREGOS_PLUGIN_SERVERS_IMAGE_PULL_SECRETS"]) == ["mirror-pull"]
    assert "CHOREGOS_PLUGIN_SERVERS_IMAGE_MIRRORS" not in _api_env(_docs(ON, tmp_path))


def test_the_rules_the_docs_list_are_the_roles(tmp_path: pathlib.Path) -> None:
    """With `rbac: false` the platform grants what docs/deployment.md lists: the template's rules."""
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["plugins"]["servers"]["egress"] = "cilium"
    role = _named(_docs(values, tmp_path), "Role", "choregos-api-plugin-servers")
    assert role is not None
    rendered = {
        (group, resource, tuple(rule["verbs"]))
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    page = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    table = page.split("**A platform that lays the Role down**", 1)[1].split("\n\n", 2)[1]
    listed = set()
    for line in table.splitlines()[2:]:
        group, resources, verbs = (cell.strip() for cell in line.strip("|").split("|"))
        for resource in resources.split(","):
            listed.add(
                (
                    group.split()[0].strip("`").strip('"'),
                    resource.strip().strip("`"),
                    tuple(v.strip() for v in verbs.split(",")),
                )
            )
    assert listed == rendered
