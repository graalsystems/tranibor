"""No pod of the chart is handed the namespace's Services as variables (finding #2057).

Kubernetes gives every pod `<SERVICE>_SERVICE_HOST`, `<SERVICE>_PORT=tcp://...` and the like for each
Service of its namespace, unless the pod says `enableServiceLinks: false`. The platform finds its
services by DNS and reads none of them; with them, a Service named `tranibor-web` became
`CHOREGOS_WEB_*` in every pod, and the R4 fallback (ADR 0176) warned about each one as an old setting.
`KUBERNETES_SERVICE_HOST`, which the clients read in a pod, is given whatever this field says.

Each environment's values, the all-in-one profile, and a render with every optional pod turned on.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"
SITE = ("global.bootstrap.org=acme", "global.bootstrap.admins=root@acme.example")
#: Every pod the defaults leave out: the fakes, the decisions service, the builder, web search and
#: its relay, the trace-drift job, the outposts' door, the canary's Rollout.
EVERYTHING = (
    "demoFakes.enabled=true",
    "demoFakes.tokenSecret.name=demo",
    "demoFakes.tokenSecret.key=token",
    "global.decisions.embedded=true",
    "global.images.buildRegistry=registry.internal/choregos-builds",
    "tranibor-websearch.enabled=true",
    "tranibor-websearch.relay.enabled=true",
    "tranibor-websearch.relay.secretName=relay-secrets",
    "tranibor-websearch.relay.organisation=acme",
    "global.tracing.otlpEndpoint=http://collector:4318",
    "global.tracing.tempoUrl=http://tempo:3200",
    "global.outposts.enabled=true",
    "global.outposts.door.dedicated=true",
    "global.outposts.door.url=https://door.example.com",
    "global.canary.enabled=true",
)


def _sets(*overrides: str) -> tuple[str, ...]:
    return tuple(a for override in overrides for a in ("--set", override))


RENDERS: dict[str, tuple[str, ...]] = {
    **{env: ("-f", str(CHART / "values" / f"{env}.yaml")) for env in ("local", "dev", "staging", "prod")},
    "all-in-one": ("-f", str(CHART / "values" / "all-in-one.yaml"), *_sets(*SITE)),
    "everything": ("-f", str(CHART / "values" / "local.yaml"), *_sets(*EVERYTHING)),
}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _pods(arguments: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", "choregos", str(CHART), "-n", "choregos", *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    pods: dict[str, dict[str, Any]] = {}
    for doc in yaml.safe_load_all(result.stdout):
        if not doc:
            continue
        kind, spec = doc.get("kind"), doc.get("spec") or {}
        name = f"{kind}/{doc['metadata']['name']}"
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job", "Rollout"}:
            pods[name] = spec["template"]["spec"]
        elif kind == "CronJob":
            pods[name] = spec["jobTemplate"]["spec"]["template"]["spec"]
        elif kind == "SandboxTemplate":
            pods[name] = spec["podTemplate"]["spec"]
        elif kind == "Pod":
            pods[name] = spec
    return pods


@pytest.mark.parametrize("render", sorted(RENDERS))
def test_every_pod_turns_service_links_off(render: str) -> None:
    pods = _pods(RENDERS[render])
    assert pods, "the render holds no pod: the test would prove nothing"
    linked = sorted(name for name, spec in pods.items() if spec.get("enableServiceLinks") is not False)
    assert linked == [], f"{render}: pods handed the namespace's Services as variables: {linked}"


def test_the_widest_render_reaches_the_optional_pods() -> None:
    """Without it, a template whose pod no render shows would escape the test above."""
    pods = set(_pods(RENDERS["everything"])) | set(_pods(RENDERS["all-in-one"]))
    assert {
        "CronJob/choregos-backup",
        "CronJob/tranibor-trace-drift",
        "Deployment/tranibor-buildkitd",
        "Deployment/tranibor-demo-fakes",
        "Deployment/choregos-egress",
        "Deployment/choregos-firecrawl",
        "Deployment/choregos-keycloak",
        "Deployment/choregos-laya",
        "Deployment/choregos-objectstore",
        "Deployment/tranibor-outposts-door",
        "Deployment/choregos-relay",
        "Deployment/choregos-searxng",
        "Job/choregos-objectstore-bucket",
        "Rollout/tranibor-api",
    } <= pods
    assert "SandboxTemplate/choregos-runner" in _pods(RENDERS["prod"])
