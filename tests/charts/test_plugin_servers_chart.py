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
from tranibor_adapters.plugin_servers import ServerDeployment, server_manifests
from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"

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
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "tranibor-api":
            env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
            return {e["name"]: e.get("value", "") for e in env}
    raise AssertionError("no tranibor-api Deployment")


def _named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next((d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name), None)


def test_off_by_default(tmp_path: pathlib.Path) -> None:
    docs = _docs({}, tmp_path)
    assert _named(docs, "Role", "tranibor-api-plugin-servers") is None
    assert not [n for n in _api_env(docs) if n.startswith("TRANIBOR_PLUGIN_SERVERS")]


def test_on_the_api_runs_servers_in_their_namespace_only(tmp_path: pathlib.Path) -> None:
    docs = _docs(ON, tmp_path)
    env = _api_env(docs)
    assert env["TRANIBOR_PLUGIN_SERVERS"] == "kubernetes"
    assert env["TRANIBOR_PLUGIN_SERVERS_NAMESPACE"] == "choregos-plugins"
    assert yaml.safe_load(env["TRANIBOR_PLUGIN_SERVERS_REGISTRIES"]) == [PUBLISHING.image("tranibor-agents")]
    assert env["TRANIBOR_PLUGIN_SERVERS_RUNTIME_CLASS"] == "gvisor", "the runner's RuntimeClass by default"
    assert env["TRANIBOR_PLUGIN_SERVERS_API_NAMESPACE"] == "choregos"
    role = _named(docs, "Role", "tranibor-api-plugin-servers")
    assert role is not None and role["metadata"]["namespace"] == "choregos-plugins"
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert resources == {"secrets", "services", "deployments", "networkpolicies"}
    assert not [
        d
        for d in docs
        if d.get("kind") in {"ClusterRole", "ClusterRoleBinding"} and "plugin" in d["metadata"]["name"]
    ]
    binding = _named(docs, "RoleBinding", "tranibor-api-plugin-servers")
    assert binding is not None
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "tranibor-api", "namespace": "choregos"}
    ]
    assert _named(docs, "Namespace", "choregos-plugins") is None, "the namespace only when asked"
    platform = _named(docs, "NetworkPolicy", "tranibor-allow-platform")
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
    role = _named(docs, "Role", "tranibor-api-plugin-servers")
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
    assert _named(docs, "Role", "tranibor-api-plugin-servers") is None
    assert _named(docs, "RoleBinding", "tranibor-api-plugin-servers") is None
    assert _api_env(docs)["TRANIBOR_PLUGIN_SERVERS"] == "kubernetes"


def test_the_pull_goes_through_the_mirror_with_its_secrets(tmp_path: pathlib.Path) -> None:
    """S80-20: the mirrors and the pull secrets reach the API; the secrets default to the release's."""
    mirror = {PUBLISHING.image("tranibor-agents"): "harbor.example/ghcr-proxy/tranibor-agents"}
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["imagePullSecrets"] = ["harbor-pull"]
    values["global"]["plugins"]["servers"]["imageMirrors"] = mirror
    env = _api_env(_docs(values, tmp_path))
    assert json.loads(env["TRANIBOR_PLUGIN_SERVERS_IMAGE_MIRRORS"]) == mirror
    assert json.loads(env["TRANIBOR_PLUGIN_SERVERS_IMAGE_PULL_SECRETS"]) == ["harbor-pull"]
    values["global"]["plugins"]["servers"]["imagePullSecrets"] = []
    assert "TRANIBOR_PLUGIN_SERVERS_IMAGE_PULL_SECRETS" not in _api_env(_docs(values, tmp_path))
    values["global"]["plugins"]["servers"]["imagePullSecrets"] = ["mirror-pull"]
    env = _api_env(_docs(values, tmp_path))
    assert json.loads(env["TRANIBOR_PLUGIN_SERVERS_IMAGE_PULL_SECRETS"]) == ["mirror-pull"]
    assert "TRANIBOR_PLUGIN_SERVERS_IMAGE_MIRRORS" not in _api_env(_docs(ON, tmp_path))


def test_the_rules_the_docs_list_are_the_roles(tmp_path: pathlib.Path) -> None:
    """With `rbac: false` the platform grants what docs/deployment.md lists: the template's rules."""
    values = yaml.safe_load(yaml.safe_dump(ON))
    values["global"]["plugins"]["servers"]["egress"] = "cilium"
    role = _named(_docs(values, tmp_path), "Role", "tranibor-api-plugin-servers")
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


def _admits(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """Whether a Kubernetes label selector selects a pod carrying `labels`."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        present, values = expression["key"] in labels, expression.get("values") or []
        held = labels.get(expression["key"])
        if expression["operator"] == "In" and held not in values:
            return False
        if expression["operator"] == "NotIn" and present and held in values:
            return False
        if expression["operator"] == "Exists" and not present:
            return False
        if expression["operator"] == "DoesNotExist" and present:
            return False
    return True


def test_every_caller_of_a_plugin_server_is_admitted_by_its_policy(tmp_path: pathlib.Path) -> None:
    """#2192: a server's NetworkPolicy admitted the API alone, while the orchestrator's workers call the
    server too (the `tracker` queue polls a `tracker/plugin`, the `orchestrator` queue plays a governed
    action's `connector.call`, and every activity builds the project's adapters). On the chart's own
    pods: the policy the API writes admits the API's and every orchestrator queue's, never a runner's,
    and the platform's egress lets each of them out to the servers' namespace."""
    docs = _docs(ON, tmp_path)
    pods = {
        doc["metadata"]["name"]: doc["spec"]["template"]["metadata"]["labels"]
        for doc in docs
        if doc.get("kind") == "Deployment"
        and doc["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name")
        in {"choregos-api", "choregos-orchestrator"}
    }
    queues = {n for n, labels in pods.items() if labels["app.kubernetes.io/name"] == "choregos-orchestrator"}
    assert len(queues) >= 4 and "tranibor-api" in pods, sorted(pods)
    server = ServerDeployment(
        name="servicenow-0123abcd",
        namespace="choregos-plugins",
        image="registry.example/s@sha256:" + "a" * 64,
        port=8080,
        health_path="/healthz",
        env={},
        labels={},
        api_namespace="choregos",
    )
    policy = next(o for o in server_manifests(server) if o["kind"] == "NetworkPolicy")
    (rule,) = policy["spec"]["ingress"]
    (source,) = rule["from"]
    assert source["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "choregos"}}
    refused = sorted(name for name, labels in pods.items() if not _admits(source["podSelector"], labels))
    assert refused == [], f"callers of a plugin server its policy does not admit: {refused}"
    assert not _admits(source["podSelector"], {"app.kubernetes.io/name": "choregos-runner"})
    platform = _named(docs, "NetworkPolicy", "tranibor-allow-platform")
    assert platform is not None
    out = sorted(
        name for name, labels in pods.items() if not _admits(platform["spec"]["podSelector"], labels)
    )
    assert out == [], f"callers the platform's egress does not let out: {out}"


def _with(**servers: Any) -> dict[str, Any]:
    values = json.loads(json.dumps(ON))
    values["global"]["plugins"]["servers"].update(servers)
    return values


def test_private_ranges_are_off_by_default(tmp_path: pathlib.Path) -> None:
    """ADR 0224 §6a: nothing listed, no variable, a private host stays refused."""
    env = _api_env(_docs(ON, tmp_path))
    assert "TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS" not in env
    assert "TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG" not in env


def test_private_ranges_reach_the_api_as_json(tmp_path: pathlib.Path) -> None:
    values = _with(
        privateCidrs=["10.20.0.0/16", "10.30.4.0/24"],
        privateCidrsByOrg={"northwind": ["10.20.0.0/16"], "contoso": ["10.30.4.0/24"]},
    )
    env = _api_env(_docs(values, tmp_path))
    assert json.loads(env["TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS"]) == ["10.20.0.0/16", "10.30.4.0/24"]
    assert json.loads(env["TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG"]) == {
        "northwind": ["10.20.0.0/16"],
        "contoso": ["10.30.4.0/24"],
    }


def test_the_values_the_chart_renders_are_the_ones_the_api_accepts_or_refuses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered variable is read by the real Settings: a closed range stops the start."""
    from pydantic import ValidationError
    from tranibor_api.config import Settings

    good = _api_env(_docs(_with(privateCidrs=["10.20.0.0/16"]), tmp_path))
    monkeypatch.setenv(
        "TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS", good["TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS"]
    )
    assert Settings().plugin_servers_egress_private_cidrs == ["10.20.0.0/16"]
    bad = _api_env(_docs(_with(privateCidrs=["169.254.0.0/16"]), tmp_path))
    monkeypatch.setenv(
        "TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS", bad["TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS"]
    )
    with pytest.raises(ValidationError):
        Settings()
