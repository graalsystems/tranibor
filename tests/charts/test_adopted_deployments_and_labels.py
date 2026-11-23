"""An embedded piece adopts the Deployment name and the pod labels a site already runs (#2150, ADR 0195).

S80-16 let a piece keep its Service name and its volume. A site that already runs `ecphoria` or `searxng`
also has a network policy that selects the pods by label, and a Deployment whose selector is immutable:
the names and the labels must be settable too, and what is not set must render as before.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")

BASE = {
    "global": {"memory": {"embedded": True}},
    "choregos-websearch": {
        "enabled": True,
        "relay": {"enabled": True, "secretName": "s", "organisation": "o"},
    },
}


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _docs(tmp_path: pathlib.Path, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    f = tmp_path / "values.yaml"
    f.write_text(yaml.safe_dump(_merge(BASE, values or {})))
    out = subprocess.run(
        ["helm", "template", "c", str(CHART), "-f", str(f)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _get(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (d,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return d


def _pod_labels(dep: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = dep["spec"]["template"]["metadata"]["labels"]
    return labels


# (piece, default Deployment name, default `app.kubernetes.io/name`, subchart values key, component)
PIECES = [
    ("ecphoria", "c-ecphoria", "choregos-ecphoria", "choregos-ecphoria", "memory"),
    ("searxng", "c-searxng", "choregos-searxng", "choregos-websearch.searxng", "websearch"),
    ("firecrawl", "c-firecrawl", "choregos-firecrawl", "choregos-websearch.firecrawl", "websearch"),
    ("redis", "c-firecrawl-redis", "choregos-firecrawl-redis", "choregos-websearch.redis", "websearch"),
    ("relay", "c-relay", "choregos-relay", "choregos-websearch.relay", "websearch"),
]


def _values(path: str, **leaf: Any) -> dict[str, Any]:
    out: dict[str, Any] = leaf
    for key in reversed(path.split(".")):
        out = {key: out}
    return out


@pytest.mark.parametrize(("piece", "deployment", "name_label", "path", "component"), PIECES)
def test_defaults_are_the_labels_and_names_the_chart_always_rendered(
    tmp_path: pathlib.Path, piece: str, deployment: str, name_label: str, path: str, component: str
) -> None:
    docs = _docs(tmp_path)
    dep = _get(docs, "Deployment", deployment)
    expected = {
        "app.kubernetes.io/name": name_label,
        "app.kubernetes.io/instance": "c",
        "app.kubernetes.io/component": component,
    }
    assert _pod_labels(dep) == expected
    assert dep["spec"]["selector"]["matchLabels"] == {
        k: v for k, v in expected.items() if "component" not in k
    }
    svc = _get(docs, "Service", deployment)
    assert svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"]


@pytest.mark.parametrize(("piece", "deployment", "name_label", "path", "component"), PIECES)
def test_selector_labels_replace_the_selector_the_pods_and_the_service_together(
    tmp_path: pathlib.Path, piece: str, deployment: str, name_label: str, path: str, component: str
) -> None:
    docs = _docs(tmp_path, _values(path, selectorLabels={"app.kubernetes.io/name": piece}))
    dep = _get(docs, "Deployment", deployment)
    assert dep["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": piece}
    assert _pod_labels(dep) == {"app.kubernetes.io/name": piece, "app.kubernetes.io/component": component}
    assert _get(docs, "Service", deployment)["spec"]["selector"] == {"app.kubernetes.io/name": piece}


@pytest.mark.parametrize(("piece", "deployment", "name_label", "path", "component"), PIECES)
def test_pod_labels_are_added_to_the_pods_and_never_to_a_selector(
    tmp_path: pathlib.Path, piece: str, deployment: str, name_label: str, path: str, component: str
) -> None:
    docs = _docs(tmp_path, _values(path, podLabels={"site/netpol": "sandbox-egress"}))
    dep = _get(docs, "Deployment", deployment)
    assert _pod_labels(dep)["site/netpol"] == "sandbox-egress"
    assert _pod_labels(dep)["app.kubernetes.io/name"] == name_label
    assert "site/netpol" not in dep["spec"]["selector"]["matchLabels"]
    assert "site/netpol" not in _get(docs, "Service", deployment)["spec"]["selector"]


def test_ecphoria_adopts_the_deployment_name_and_the_volume_keeps_its_own(tmp_path: pathlib.Path) -> None:
    docs = _docs(tmp_path, {"choregos-ecphoria": {"deploymentName": "ecphoria"}})
    dep = _get(docs, "Deployment", "ecphoria")
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    (data,) = [v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "data"]
    assert data["persistentVolumeClaim"]["claimName"] == "c-ecphoria"
    assert not [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-ecphoria"]
    _get(docs, "PersistentVolumeClaim", "c-ecphoria")


def test_the_charts_own_network_policy_follows_the_adopted_ecphoria_label(tmp_path: pathlib.Path) -> None:
    docs = _docs(tmp_path, {"choregos-ecphoria": {"selectorLabels": {"app.kubernetes.io/name": "ecphoria"}}})
    peers = [
        p["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        for d in docs
        if d["kind"] == "NetworkPolicy"
        for p in d["spec"].get("egress", [])
        if p.get("to") and "podSelector" in p["to"][0] and 8432 in [q.get("port") for q in p.get("ports", [])]
    ]
    assert peers
    assert set(peers) == {"ecphoria"}


def test_websearch_deployments_follow_the_adopted_names(tmp_path: pathlib.Path) -> None:
    docs = _docs(
        tmp_path, {"choregos-websearch": {"names": {"searxng": "searxng", "relay": "choregos-relay"}}}
    )
    _get(docs, "Deployment", "searxng")
    _get(docs, "Deployment", "choregos-relay")
    _get(docs, "Service", "searxng")


def test_the_searxng_restart_checksum_ignores_the_labels(tmp_path: pathlib.Path) -> None:
    """Adopting labels must not restart a SearXNG whose settings did not change."""

    def checksum(values: dict[str, Any]) -> str:
        dep = _get(_docs(tmp_path, values), "Deployment", "c-searxng")
        return str(dep["spec"]["template"]["metadata"]["annotations"]["checksum/settings"])

    plain = checksum({})
    assert checksum(_values("choregos-websearch.searxng", podLabels={"a": "b"})) == plain
    assert checksum(_values("choregos-websearch.searxng", instanceName="other")) != plain
