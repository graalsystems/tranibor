# SPDX-License-Identifier: Apache-2.0
"""`tranibor cluster check`: what the chart needs, asked of the cluster before the first install (S86-03).

The chart can only assume what a cluster gives: `helm template` and Argo CD render offline, and a
`lookup` answers nothing there. What it assumes is asked here, of the API server the operator's
kubeconfig names, before anything is installed: the Kubernetes version (native sidecars, 1.29), the
agent-sandbox operator when `agent_sandbox` runs the agents, the named RuntimeClass, a default
StorageClass, the monitoring CRDs, and two things only a pod can tell: that a NetworkPolicy is
**enforced** (a denied pod really is denied, not only accepted by the API server), and that the
egress gate closes for a new runner pod within its deadline.

Read-only but for one namespace of its own (`tranibor-preflight-…`): a policy and three throw-away
pods, deleted with the namespace at the end, failure or not. `--read-only` skips them.

Every check says what it found, why the platform needs it, and the value or the setting that fixes
it; a check that does not apply to these settings says so. Exit code 1 on any failure.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer
import yaml
from rich.console import Console
from tranibor_core.egress_gate import (
    DEFAULT_CLOSED_STREAK,
    DEFAULT_DEADLINE_S,
    DEFAULT_INTERVAL_S,
    DEFAULT_TARGET,
    DEFAULT_TIMEOUT_S,
    parse_target,
)
from tranibor_core.hosted import NATIVE_SIDECARS_MIN, kubernetes_version

from . import kube
from .kube import KubeApi, KubeApiError, KubeConfigError

cluster_app = typer.Typer(help="The cluster the platform is installed on")
console = Console()

#: The agent-sandbox release the platform is tested against, as `infra/operators/agent-sandbox/
#: kustomization.yaml` pins it (`packages/cli/tests/test_cluster_check.py` keeps the two in step).
AGENT_SANDBOX_RELEASE = "v1.0.6"
AGENT_SANDBOX_DIGEST = "sha256:14c479c9e89ca87d05dfcf597840473c0196d6e6eec622a7959e72108d75b24e"
AGENT_SANDBOX_IMAGE = "agent-sandbox-controller"
#: The API groups the executor speaks (`tranibor_adapters.executor.agent_sandbox`) and what each serves.
AGENT_SANDBOX_APIS: dict[str, frozenset[str]] = {
    "agents.x-k8s.io/v1beta1": frozenset({"sandboxes"}),
    "extensions.agents.x-k8s.io/v1beta1": frozenset(
        {"sandboxtemplates", "sandboxclaims", "sandboxwarmpools"}
    ),
}
#: What `monitoring.flavour` renders (`charts/choregos/templates/monitoring.yaml`).
MONITORING_APIS: dict[str, tuple[str, str, str]] = {
    # flavour: (group/version, scrape resource, rule resource)
    "prometheus": ("monitoring.coreos.com/v1", "servicemonitors", "prometheusrules"),
    "victoriametrics": ("operator.victoriametrics.com/v1beta1", "vmservicescrapes", "vmrules"),
}
DEFAULT_STORAGE_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.beta.kubernetes.io/is-default-class",
)
#: The probe pods' image: the Kubernetes project's own network test image (its NetworkPolicy e2e
#: tests use it), with a shell, `nc` and an HTTP server. `--probe-image` names a mirror.
PROBE_IMAGE = "registry.k8s.io/e2e-test-images/agnhost:2.56"
SERVER_PORT = 8080
#: The labels of every pod an agent runs in (`tranibor_adapters.executor.k8s_job.RUNNER_POD_LABELS`,
#: the warm pool's template too): the chart's `choregos-runner-egress` selects them.
RUNNER_POD_LABELS = {
    "app.kubernetes.io/name": "choregos-runner",
    "app.kubernetes.io/component": "runner",
    "choregos/sandbox": "true",
}
MANAGED_BY = {"app.kubernetes.io/managed-by": "tranibor-cluster-check"}


class Status(StrEnum):
    OK = "ok"
    FAIL = "fail"
    NOT_APPLICABLE = "not applicable"


@dataclass(frozen=True)
class Result:
    """One check: what it found (`detail`), why the platform needs it, and what fixes a failure."""

    id: str
    title: str
    status: Status
    detail: str
    why: str
    fix: str = ""


@dataclass(frozen=True)
class Settings:
    """What the installation will ask of the cluster: the chart's values, or flags."""

    executor: str = "agent_sandbox"
    runtime_class: str = ""
    monitoring_flavour: str = "prometheus"
    service_monitor: bool = True
    alerts: bool = True
    object_store_endpoint: str = ""
    gate_enabled: bool = True
    gate_target: str = DEFAULT_TARGET
    gate_deadline_s: float = DEFAULT_DEADLINE_S
    probe_image: str = PROBE_IMAGE
    read_only: bool = False
    pod_timeout_s: float = 180.0


def _dig(values: Mapping[str, Any], *path: str) -> Any:
    node: Any = values
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _deep_merge(base: dict[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in top.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def settings_from_values(files: Sequence[Path]) -> dict[str, Any]:
    """The settings the chart's values files name, merged as Helm does (the last file wins).

    Only the keys present are returned: what a file leaves out keeps the chart's default."""
    merged: dict[str, Any] = {}
    for path in files:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise typer.BadParameter(f"{path} is not a mapping of values")
        _deep_merge(merged, document)
    found: dict[str, Any] = {}
    if (executor := _dig(merged, "global", "runner", "executor")) is not None:
        found["executor"] = str(executor)
    runtime_class = _dig(merged, "global", "runner", "runtimeClass")
    if runtime_class is None:  # the pool's own setting, which the global one replaces (values.yaml)
        runtime_class = _dig(merged, "choregos-orchestrator", "runner", "runtimeClass")
    if runtime_class is not None:
        found["runtime_class"] = str(runtime_class)
    for key, name in (
        ("flavour", "monitoring_flavour"),
        ("serviceMonitor", "service_monitor"),
        ("alerts", "alerts"),
    ):
        if (value := _dig(merged, "monitoring", key)) is not None:
            found[name] = value if isinstance(value, bool) else str(value)
    store = _dig(merged, "global", "objectStore") or {}
    if isinstance(store, Mapping) and store.get("endpoint") and not store.get("embedded"):
        found["object_store_endpoint"] = str(store["endpoint"])
    gate = _dig(merged, "choregos-orchestrator", "runner", "egressGate") or {}
    if isinstance(gate, Mapping):
        if "enabled" in gate:
            found["gate_enabled"] = bool(gate["enabled"])
        if gate.get("target"):
            found["gate_target"] = str(gate["target"])
        if gate.get("deadlineSeconds") is not None:
            found["gate_deadline_s"] = float(gate["deadlineSeconds"])
    return found


# --- The checks that only read ---------------------------------------------------------------------


async def check_version(api: KubeApi) -> Result:
    body = await api.get("/version") or {}
    shown = str(body.get("gitVersion") or f"{body.get('major')}.{body.get('minor')}")
    parsed = kubernetes_version(body)
    wanted = ".".join(map(str, NATIVE_SIDECARS_MIN))
    why = (
        f"the agent's own container (`agentIsolation: container`, the default) and a hosted agent run as "
        f"native sidecars, which Kubernetes has from {wanted}"
    )
    if parsed is None:
        return Result(
            "kubernetes-version",
            "Kubernetes version",
            Status.FAIL,
            f"unreadable: {shown!r}",
            why,
            f"check `kubectl version`; the platform needs {wanted} or later",
        )
    if parsed < NATIVE_SIDECARS_MIN:
        return Result(
            "kubernetes-version",
            "Kubernetes version",
            Status.FAIL,
            f"{shown}, older than {wanted}",
            why,
            f"upgrade the cluster to {wanted} or later (on a managed cloud, the control plane's version "
            "setting, then the node pools)",
        )
    return Result("kubernetes-version", "Kubernetes version", Status.OK, f"{shown} (≥ {wanted})", why)


async def _served(api: KubeApi, group_version: str) -> set[str]:
    """The resources an API group version serves; empty when the cluster does not serve it."""
    body = await api.get(f"/apis/{group_version}")
    return {
        str(item.get("name"))
        for item in (body or {}).get("resources") or []
        if "/" not in str(item.get("name"))
    }


async def check_agent_sandbox(api: KubeApi, settings: Settings) -> list[Result]:
    why = (
        "`global.runner.executor: agent_sandbox` (the chart's default) runs every agent in a warm pool "
        "of the kubernetes-sigs/agent-sandbox operator; the chart refuses to render without its CRDs"
    )
    install = (
        f"install the operator, {AGENT_SANDBOX_RELEASE} (`infra/operators/agent-sandbox`; "
        'docs/deployment.md, "The `agent_sandbox` executor"), or run agents as Jobs: '
        "`global.runner.executor: k8s_job`"
    )
    if settings.executor != "agent_sandbox":
        skipped = f"the executor is `{settings.executor}`"
        return [
            Result("agent-sandbox-crds", "agent-sandbox CRDs", Status.NOT_APPLICABLE, skipped, why),
            Result(
                "agent-sandbox-version", "agent-sandbox operator version", Status.NOT_APPLICABLE, skipped, why
            ),
        ]
    missing = []
    for group_version, wanted in AGENT_SANDBOX_APIS.items():
        lacking = sorted(wanted - await _served(api, group_version))
        missing += [f"{group_version} {name}" for name in lacking]
    if missing:
        crds = Result(
            "agent-sandbox-crds",
            "agent-sandbox CRDs",
            Status.FAIL,
            "not served: " + ", ".join(missing),
            why,
            install,
        )
    else:
        crds = Result(
            "agent-sandbox-crds",
            "agent-sandbox CRDs",
            Status.OK,
            "served: " + ", ".join(AGENT_SANDBOX_APIS),
            why,
        )
    return [crds, await _agent_sandbox_version(api, why)]


async def _agent_sandbox_version(api: KubeApi, why: str) -> Result:
    title = "agent-sandbox operator version"
    pin = f"{AGENT_SANDBOX_RELEASE} (`infra/operators/agent-sandbox/kustomization.yaml`)"
    fix = (
        f"install {pin}: the executor speaks its API, and a release that moved under it fails in a run, "
        "not here"
    )
    try:
        body = await api.get("/apis/apps/v1/deployments")
    except KubeApiError as exc:
        return Result(
            "agent-sandbox-version",
            title,
            Status.FAIL,
            f"cannot list Deployments to find the controller ({exc.status})",
            why,
            "run the check with a context that may list Deployments in every namespace",
        )
    images = [
        (item["metadata"]["namespace"], container.get("image", ""))
        for item in (body or {}).get("items") or []
        for container in (item.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [])
        if AGENT_SANDBOX_IMAGE in container.get("image", "")
    ]
    if not images:
        return Result(
            "agent-sandbox-version",
            title,
            Status.FAIL,
            f"no Deployment runs `{AGENT_SANDBOX_IMAGE}`",
            why,
            fix,
        )
    namespace, image = images[0]
    if image.endswith(f":{AGENT_SANDBOX_RELEASE}") or AGENT_SANDBOX_DIGEST in image:
        return Result("agent-sandbox-version", title, Status.OK, f"{image} in `{namespace}` = {pin}", why)
    return Result(
        "agent-sandbox-version",
        title,
        Status.FAIL,
        f"{image} in `{namespace}`, not the pinned {AGENT_SANDBOX_RELEASE}",
        why,
        fix,
    )


async def check_runtime_class(api: KubeApi, settings: Settings) -> Result:
    why = (
        "a pod naming a RuntimeClass the cluster lacks is never admitted: every run would hang before "
        "its first step"
    )
    if not settings.runtime_class:
        return Result(
            "runtime-class",
            "RuntimeClass",
            Status.NOT_APPLICABLE,
            "no RuntimeClass named (`global.runner.runtimeClass`): the cluster's default runtime",
            why,
        )
    if await api.get(f"/apis/node.k8s.io/v1/runtimeclasses/{settings.runtime_class}") is not None:
        return Result("runtime-class", "RuntimeClass", Status.OK, f"`{settings.runtime_class}` exists", why)
    listed = await api.get("/apis/node.k8s.io/v1/runtimeclasses") or {}
    names = sorted(str(item["metadata"]["name"]) for item in listed.get("items") or [])
    return Result(
        "runtime-class",
        "RuntimeClass",
        Status.FAIL,
        f"`{settings.runtime_class}` does not exist (the cluster has: {', '.join(names) or 'none'})",
        why,
        "install the sandboxed runtime and its RuntimeClass (gVisor: `infra/bootstrap/gvisor.yaml`; a "
        "managed cloud: its sandboxed node pool), set `global.runner.runtimeClass` to a class listed "
        "here, or leave it unset",
    )


async def check_storage_class(api: KubeApi) -> Result:
    why = (
        "the chart's volumes (an embedded database, the object store, a warm sandbox) are claims with no "
        "`storageClassName`: without a default StorageClass they stay Pending"
    )
    listed = await api.get("/apis/storage.k8s.io/v1/storageclasses") or {}
    items = listed.get("items") or []
    defaults = [
        str(item["metadata"]["name"])
        for item in items
        if any(
            str((item["metadata"].get("annotations") or {}).get(key, "")).lower() == "true"
            for key in DEFAULT_STORAGE_ANNOTATIONS
        )
    ]
    if defaults:
        return Result(
            "default-storage-class",
            "default StorageClass",
            Status.OK,
            f"`{defaults[0]}`" + (f" (and {len(defaults) - 1} more)" if len(defaults) > 1 else ""),
            why,
        )
    names = [str(item["metadata"]["name"]) for item in items]
    target = names[0] if names else "<name>"
    return Result(
        "default-storage-class",
        "default StorageClass",
        Status.FAIL,
        f"none marked default (the cluster has: {', '.join(names) or 'none'})",
        why,
        f"mark one default: `kubectl patch storageclass {target} -p "
        '\'{"metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}}\'`, '
        "or install a provisioner (k3s: local-path; a managed cloud: its CSI driver's class)",
    )


async def check_monitoring(api: KubeApi, settings: Settings) -> Result:
    why = (
        "`monitoring.serviceMonitor` and `monitoring.alerts` (on by default) render objects of the "
        "monitoring operator's CRDs: without them `helm install` fails on an unknown kind"
    )
    if not (settings.service_monitor or settings.alerts):
        return Result(
            "monitoring-crds",
            "monitoring CRDs",
            Status.NOT_APPLICABLE,
            "`monitoring.serviceMonitor` and `monitoring.alerts` are off",
            why,
        )
    if settings.monitoring_flavour not in MONITORING_APIS:
        return Result(
            "monitoring-crds",
            "monitoring CRDs",
            Status.FAIL,
            f"unknown `monitoring.flavour: {settings.monitoring_flavour}`",
            why,
            "`monitoring.flavour: prometheus` or `victoriametrics`",
        )
    group_version, scrape, rule = MONITORING_APIS[settings.monitoring_flavour]
    wanted = {scrape} if settings.service_monitor else set()
    wanted |= {rule} if settings.alerts else set()
    lacking = sorted(wanted - await _served(api, group_version))
    if not lacking:
        return Result(
            "monitoring-crds",
            "monitoring CRDs",
            Status.OK,
            f"served: {group_version} {', '.join(sorted(wanted))}",
            why,
        )
    operator = (
        "the Prometheus Operator"
        if settings.monitoring_flavour == "prometheus"
        else "the VictoriaMetrics operator"
    )
    return Result(
        "monitoring-crds",
        "monitoring CRDs",
        Status.FAIL,
        f"not served: {group_version} {', '.join(lacking)}",
        why,
        f"install {operator}'s CRDs, or set `monitoring.serviceMonitor: false` and "
        "`monitoring.alerts: false`",
    )


# --- The checks a pod answers ----------------------------------------------------------------------

#: The fenced probe: the egress gate's own loop (`tranibor_core.egress_gate.wait_until_closed`), then
#: the same loop against the server pod the policy forbids. One `key=value` line each.
FENCED_SCRIPT = r"""
now() { cut -d' ' -f1 /proc/uptime; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.2f", b - a }'; }
past() { awk -v e="$1" -v d="$2" 'BEGIN { exit !(e >= d) }'; }
probe() { nc -w "$TIMEOUT" "$1" "$2" </dev/null >/dev/null 2>&1; }
until_closed() {
  start=$(now); attempts=0; streak=0; opened=0; at=""
  while :; do
    attempts=$((attempts + 1))
    if probe "$1" "$2"; then streak=0; opened=$((opened + 1)); at="$at,$(since "$start")"
    else streak=$((streak + 1)); fi
    if [ "$streak" -ge "$STREAK" ] || past "$(since "$start")" "$DEADLINE"; then break; fi
    sleep "$INTERVAL"
  done
  if [ "$streak" -ge "$STREAK" ]; then outcome=closed; else outcome=open; fi
  echo "$3=$outcome waited=$(since "$start") attempts=$attempts open_attempts=$opened open_at=${at#,}"
}
if [ -n "$GATE_HOST" ]; then until_closed "$GATE_HOST" "$GATE_PORT" gate; fi
until_closed "$SERVER_HOST" "$SERVER_PORT" server
"""
#: The open probe, under no policy: is the server reachable at all, the gate's target, the store?
OPEN_SCRIPT = r"""
reach() {
  i=0
  while [ "$i" -lt "$3" ]; do
    if nc -w 2 "$1" "$2" </dev/null >/dev/null 2>&1; then echo open; return; fi
    i=$((i + 1)); sleep 1
  done
  echo closed
}
echo "server=$(reach "$SERVER_HOST" "$SERVER_PORT" 30)"
if [ -n "$GATE_HOST" ]; then echo "gate=$(reach "$GATE_HOST" "$GATE_PORT" 3)"; fi
if [ -n "$STORE_HOST" ]; then echo "store=$(reach "$STORE_HOST" "$STORE_PORT" 3)"; fi
"""


def parse_report(logs: str) -> dict[str, dict[str, str]]:
    """`key=outcome a=b c=d` lines into `{key: {"outcome": …, "a": …}}`; anything else is ignored."""
    report: dict[str, dict[str, str]] = {}
    for line in logs.splitlines():
        words = line.strip().split()
        if not words or "=" not in words[0]:
            continue
        key, _, outcome = words[0].partition("=")
        fields = dict(word.partition("=")[::2] for word in words[1:] if "=" in word)
        report[key] = {"outcome": outcome, **fields}
    return report


def _store_address(endpoint: str) -> tuple[str, int]:
    parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not parts.hostname:
        raise ValueError(f"the object store endpoint {endpoint!r} names no host")
    return parts.hostname, parts.port or (80 if parts.scheme == "http" else 443)


def _pod(
    name: str,
    labels: Mapping[str, str],
    image: str,
    command: list[str],
    env: Mapping[str, str],
    ready_port: int = 0,
) -> dict[str, Any]:
    """A pod the `restricted` Pod Security level admits, small enough for any quota. `ready_port`: it is
    Ready once that port accepts (the server, which a probe must not find closed for being early)."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {**MANAGED_BY, **labels}},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 0,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "command": command,
                    "env": [{"name": key, "value": value} for key, value in env.items()],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "200m", "memory": "64Mi"},
                    },
                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    **(
                        {"readinessProbe": {"tcpSocket": {"port": ready_port}, "periodSeconds": 1}}
                        if ready_port
                        else {}
                    ),
                }
            ],
        },
    }


#: The chart's `choregos-runner-egress` once the default deny holds: DNS to `kube-system`, and in this
#: namespace nothing else (the API, the gateway and the egress proxy it also opens are not here).
def _runner_policy() -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "preflight-runner-egress", "labels": dict(MANAGED_BY)},
        "spec": {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": RUNNER_POD_LABELS["app.kubernetes.io/name"]}
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
                    ],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                }
            ],
        },
    }


Sleep = Callable[[float], Awaitable[None]]


async def _wait_pod(
    api: KubeApi, namespace: str, name: str, *, until: set[str], timeout_s: float, sleep: Sleep
) -> dict[str, Any]:
    """The pod once its phase is one of `until`; `RuntimeError` naming why it is not, at the deadline."""
    deadline = time.monotonic() + timeout_s
    pod: dict[str, Any] = {}
    while True:
        pod = await api.get(f"/api/v1/namespaces/{namespace}/pods/{name}") or {}
        status = pod.get("status") or {}
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        if status.get("phase") in until and (status.get("phase") != "Running" or ready):
            return pod
        if status.get("phase") == "Failed" or time.monotonic() >= deadline:
            break
        await sleep(1.0)
    waiting = [
        f"{state.get('reason')}: {state.get('message', '')}".strip(": ")
        for container in (pod.get("status") or {}).get("containerStatuses") or []
        if (state := (container.get("state") or {}).get("waiting"))
    ]
    phase = (pod.get("status") or {}).get("phase", "unknown")
    raise RuntimeError(
        f"the probe pod `{name}` did not get there (phase {phase}"
        f"{'; ' + '; '.join(waiting) if waiting else ''})"
    )


async def network_checks(api: KubeApi, settings: Settings, *, sleep: Sleep = asyncio.sleep) -> list[Result]:
    """NetworkPolicy enforced, the egress gate, the object store: three pods in a namespace of their own."""
    enforce_why = (
        "every promise about an agent's network (default-deny egress, the runner's own policy, the "
        "egress proxy as the only way out) is a NetworkPolicy: a cluster that accepts policies "
        "without enforcing them runs agents unfenced"
    )
    gate_why = (
        "every agent pod starts with the egress gate, which exits 115 and fails the run when a direct "
        f"connection to `{settings.gate_target}` is still possible after {settings.gate_deadline_s:g} s"
    )
    store_why = (
        "the API stores file artifacts in the object store; a store it cannot reach fails every upload"
    )
    store_endpoint = settings.object_store_endpoint
    skipped_store = Result(
        "object-store",
        "object store reachable",
        Status.NOT_APPLICABLE,
        "no endpoint (`--object-store-endpoint`, `global.objectStore.endpoint`)",
        store_why,
    )
    if settings.read_only:
        reason = "`--read-only`: no probe pod was started"
        results = [
            Result(
                "network-policy-enforced",
                "NetworkPolicy enforced",
                Status.NOT_APPLICABLE,
                reason,
                enforce_why,
            ),
            Result("egress-gate", "egress gate closes", Status.NOT_APPLICABLE, reason, gate_why),
        ]
        return [*results, replace_detail(skipped_store, reason) if store_endpoint else skipped_store]

    gate_host, gate_port = parse_target(settings.gate_target) if settings.gate_enabled else ("", 0)
    store_host, store_port = _store_address(store_endpoint) if store_endpoint else ("", 0)
    namespace = f"tranibor-preflight-{secrets.token_hex(3)}"
    base = f"/api/v1/namespaces/{namespace}"
    await api.create(
        "/api/v1/namespaces",
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": dict(MANAGED_BY)},
        },
    )
    try:
        try:
            await api.create(
                f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies", _runner_policy()
            )
        except KubeApiError as exc:
            refused = Result(
                "network-policy-enforced",
                "NetworkPolicy enforced",
                Status.FAIL,
                f"the API server refused the policy ({exc})",
                enforce_why,
                "a cluster that serves `networking.k8s.io/v1` NetworkPolicy, with a CNI that enforces it",
            )
            return [
                refused,
                Result(
                    "egress-gate",
                    "egress gate closes",
                    Status.FAIL,
                    "not probed: the policy was refused",
                    gate_why,
                ),
                skipped_store,
            ]
        await api.create(
            f"{base}/pods",
            _pod(
                "server",
                {"role": "server"},
                settings.probe_image,
                ["/agnhost", "netexec", f"--http-port={SERVER_PORT}", "--udp-port=-1"],
                {},
                SERVER_PORT,
            ),
        )
        server = await _wait_pod(
            api, namespace, "server", until={"Running"}, timeout_s=settings.pod_timeout_s, sleep=sleep
        )
        server_ip = str((server.get("status") or {}).get("podIP") or "")
        env = {
            "SERVER_HOST": server_ip,
            "SERVER_PORT": str(SERVER_PORT),
            "GATE_HOST": gate_host,
            "GATE_PORT": str(gate_port),
            "STORE_HOST": store_host,
            "STORE_PORT": str(store_port),
            "TIMEOUT": f"{DEFAULT_TIMEOUT_S:g}",
            "INTERVAL": f"{DEFAULT_INTERVAL_S:g}",
            "STREAK": str(DEFAULT_CLOSED_STREAK),
            "DEADLINE": f"{settings.gate_deadline_s:g}",
        }
        await api.create(
            f"{base}/pods",
            _pod("open", {"role": "open"}, settings.probe_image, ["sh", "-c", OPEN_SCRIPT], env),
        )
        await api.create(
            f"{base}/pods",
            _pod("fenced", RUNNER_POD_LABELS, settings.probe_image, ["sh", "-c", FENCED_SCRIPT], env),
        )
        reports = {}
        for name in ("open", "fenced"):
            await _wait_pod(
                api,
                namespace,
                name,
                until={"Succeeded"},
                timeout_s=settings.pod_timeout_s + 2 * settings.gate_deadline_s + 60,
                sleep=sleep,
            )
            reports[name] = parse_report(str(await api.get(f"{base}/pods/{name}/log") or ""))
    except (RuntimeError, KubeApiError) as exc:
        reason = f"not probed: {exc}"
        fix = f"`--probe-image` names a mirror of `{PROBE_IMAGE}` the nodes can pull, or `--read-only`"
        return [
            Result(
                "network-policy-enforced", "NetworkPolicy enforced", Status.FAIL, reason, enforce_why, fix
            ),
            Result("egress-gate", "egress gate closes", Status.FAIL, reason, gate_why, fix),
            replace_detail(skipped_store, reason) if store_endpoint else skipped_store,
        ]
    finally:
        try:
            await api.delete(f"/api/v1/namespaces/{namespace}")
        except (KubeApiError, httpx.HTTPError):
            console.print(
                f"[yellow]the namespace `{namespace}` was not deleted: "
                f"`kubectl delete namespace {namespace}`[/yellow]"
            )
    return [
        _enforcement(reports, enforce_why),
        _gate(reports, settings, gate_why),
        _store(reports, store_endpoint, store_why) if store_endpoint else skipped_store,
    ]


def replace_detail(result: Result, detail: str) -> Result:
    return Result(result.id, result.title, result.status, detail, result.why, result.fix)


def _enforcement(reports: Mapping[str, Mapping[str, Mapping[str, str]]], why: str) -> Result:
    title = "NetworkPolicy enforced"
    baseline = reports["open"].get("server", {}).get("outcome")
    fenced = reports["fenced"].get("server", {})
    if baseline != "open":
        return Result(
            "network-policy-enforced",
            title,
            Status.FAIL,
            "inconclusive: a pod under no policy could not reach the server pod either",
            why,
            "pods must reach each other without a policy: check the CNI (`kubectl -n kube-system get pods`)",
        )
    if fenced.get("outcome") == "closed":
        return Result(
            "network-policy-enforced",
            title,
            Status.OK,
            "a pod under no policy reached the server pod; a pod the accepted deny policy selects did "
            f"not ({fenced.get('waited', '?')} s to three failed attempts in a row)",
            why,
        )
    return Result(
        "network-policy-enforced",
        title,
        Status.FAIL,
        "the API server accepted a deny policy, and the pod it denies still reached the server pod: "
        "NetworkPolicy is not enforced",
        why,
        "a CNI that enforces NetworkPolicy: Calico or Cilium (kind: `disableDefaultCNI` and Calico, as "
        "`make cluster-up`); k3s without `--disable-network-policy`; EKS: the VPC CNI's network policy "
        "agent (`enableNetworkPolicy`); GKE: Dataplane V2 or network policy enforcement on; AKS: "
        "`--network-policy` azure, calico or cilium",
    )


def _gate(reports: Mapping[str, Mapping[str, Mapping[str, str]]], settings: Settings, why: str) -> Result:
    title = "egress gate closes"
    if not settings.gate_enabled:
        return Result(
            "egress-gate",
            title,
            Status.NOT_APPLICABLE,
            "the gate is off (`choregos-orchestrator.runner.egressGate.enabled: false`)",
            why,
        )
    if reports["open"].get("gate", {}).get("outcome") != "open":
        return Result(
            "egress-gate",
            title,
            Status.NOT_APPLICABLE,
            f"`{settings.gate_target}` is not reachable even from a pod under no policy: the gate passes at "
            "once and proves nothing about the policy here",
            why,
            "point `choregos-orchestrator.runner.egressGate.target` (and `--gate-target`) at an address that "
            "answers and that the runner pods' policy refuses",
        )
    gate = reports["fenced"].get("gate", {})
    if gate.get("outcome") == "closed":
        opened = gate.get("open_attempts", "0")
        window = (
            f"; open at {gate.get('open_at')} s before the policy held" if opened not in {"", "0"} else ""
        )
        return Result(
            "egress-gate",
            title,
            Status.OK,
            f"closed after {gate.get('waited', '?')} s, {opened} attempt(s) got through{window}",
            why,
        )
    return Result(
        "egress-gate",
        title,
        Status.FAIL,
        f"a runner pod still reached `{settings.gate_target}` after {settings.gate_deadline_s:g} s: "
        "every agent pod would exit 115",
        why,
        "a NetworkPolicy engine that applies a new pod's policy within the deadline (see the check "
        "above), or a longer `choregos-orchestrator.runner.egressGate.deadlineSeconds`",
    )


def _store(reports: Mapping[str, Mapping[str, Mapping[str, str]]], endpoint: str, why: str) -> Result:
    title = "object store reachable"
    if reports["open"].get("store", {}).get("outcome") == "open":
        return Result(
            "object-store",
            title,
            Status.OK,
            f"`{endpoint}` answers a pod under no policy; once installed, the API's egress must open it "
            "too (`networkPolicies.platformEgress`)",
            why,
        )
    return Result(
        "object-store",
        title,
        Status.FAIL,
        f"`{endpoint}` does not answer a pod in the cluster",
        why,
        "the endpoint and port the pods can reach (`global.objectStore.endpoint`), and the firewall that "
        "stands between them opened",
    )


async def run_checks(api: KubeApi, settings: Settings, *, sleep: Sleep = asyncio.sleep) -> list[Result]:
    results = [await check_version(api)]
    results += await check_agent_sandbox(api, settings)
    results.append(await check_runtime_class(api, settings))
    results.append(await check_storage_class(api))
    results.append(await check_monitoring(api, settings))
    results += await network_checks(api, settings, sleep=sleep)
    return results


# --- The command ------------------------------------------------------------------------------------

GLYPHS = {Status.OK: "[green]✓[/green]", Status.FAIL: "[red]✗[/red]", Status.NOT_APPLICABLE: "[dim]–[/dim]"}


@dataclass
class _Run:
    context: str
    results: list[Result] = field(default_factory=list)


async def _check(settings: Settings, kubeconfig: str | None, context: str | None, server: str | None) -> _Run:
    conn = await kube.connection(kubeconfig, context, server)
    api = KubeApi(conn)
    try:
        return _Run(conn.context, await run_checks(api, settings))
    finally:
        await api.aclose()


@cluster_app.command("check")
def check(  # noqa: PLR0913 - one option per setting the chart reads
    values: Annotated[
        list[Path] | None,
        typer.Option(
            "--values",
            "-f",
            help="the chart's values file(s), as `helm -f`: the settings below are read from them",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    executor: Annotated[
        str | None, typer.Option(help="`global.runner.executor` (default `agent_sandbox`)")
    ] = None,
    runtime_class: Annotated[
        str | None, typer.Option("--runtime-class", help="`global.runner.runtimeClass`")
    ] = None,
    monitoring: Annotated[
        bool | None,
        typer.Option(
            "--monitoring/--no-monitoring",
            help="`monitoring.serviceMonitor` and `monitoring.alerts` (default on)",
        ),
    ] = None,
    monitoring_flavour: Annotated[
        str | None,
        typer.Option("--monitoring-flavour", help="`monitoring.flavour`: prometheus or victoriametrics"),
    ] = None,
    object_store_endpoint: Annotated[
        str | None,
        typer.Option(
            "--object-store-endpoint", help="`global.objectStore.endpoint`: probed from a pod when given"
        ),
    ] = None,
    gate: Annotated[
        bool | None,
        typer.Option(
            "--gate/--no-gate", help="`choregos-orchestrator.runner.egressGate.enabled` (default on)"
        ),
    ] = None,
    gate_target: Annotated[
        str | None, typer.Option("--gate-target", help="the egress gate's `target` (default 1.1.1.1:443)")
    ] = None,
    gate_deadline: Annotated[
        float | None, typer.Option("--gate-deadline", help="the egress gate's `deadlineSeconds` (default 30)")
    ] = None,
    probe_image: Annotated[
        str,
        typer.Option(
            "--probe-image",
            help="the probe pods' image (a mirror where the nodes cannot pull registry.k8s.io)",
        ),
    ] = PROBE_IMAGE,
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="start no probe pod: the network checks are then not applicable"),
    ] = False,
    kubeconfig: Annotated[
        str | None,
        typer.Option("--kubeconfig", help="the kubeconfig (default: $KUBECONFIG, then ~/.kube/config)"),
    ] = None,
    context: Annotated[
        str | None, typer.Option("--context", help="the kubeconfig context (default: its current one)")
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="an API server that needs no credentials, `kubectl proxy`'s address"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="print the results as JSON")] = False,
) -> None:
    """What the chart needs, asked of the cluster before installing: exit 1 when anything fails.

    Read-only but for one namespace of its own (a NetworkPolicy and three small pods), deleted at the end.
    """
    found = settings_from_values(values or [])
    flags: dict[str, Any] = {
        "executor": executor,
        "runtime_class": runtime_class,
        "monitoring_flavour": monitoring_flavour,
        "object_store_endpoint": object_store_endpoint,
        "gate_enabled": gate,
        "gate_target": gate_target,
        "gate_deadline_s": gate_deadline,
    }
    if monitoring is not None:
        flags["service_monitor"] = flags["alerts"] = monitoring
    found.update({key: value for key, value in flags.items() if value is not None})
    try:
        settings = Settings(**found, probe_image=probe_image, read_only=read_only)
        if settings.gate_enabled:
            parse_target(settings.gate_target)
        if settings.object_store_endpoint:
            _store_address(settings.object_store_endpoint)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        run = asyncio.run(_check(settings, kubeconfig, context, server))
    except KubeConfigError as exc:
        console.print(f"[red]✗[/red] {exc}", markup=True, highlight=False)
        raise typer.Exit(2) from exc
    except (KubeApiError, httpx.HTTPError) as exc:
        console.print(f"[red]✗[/red] the API server refused or did not answer: {exc}", highlight=False)
        raise typer.Exit(2) from exc
    failed = [result for result in run.results if result.status is Status.FAIL]
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "context": run.context,
                    "ok": not failed,
                    "checks": [{**asdict(result), "status": str(result.status)} for result in run.results],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        console.print(f"Cluster: [bold]{run.context}[/bold]", highlight=False)
        for result in run.results:
            console.print(f"{GLYPHS[result.status]} {result.title}: ", end="", highlight=False)
            console.print(result.detail, markup=False, highlight=False)
            if result.status is Status.FAIL:
                console.print(f"    why: {result.why}", markup=False, highlight=False)
                if result.fix:
                    console.print(f"    fix: {result.fix}", markup=False, highlight=False)
        verdict = (
            f"[red]{len(failed)} check(s) failed[/red]"
            if failed
            else "[green]the cluster has what the chart needs[/green]"
        )
        console.print(verdict)
    if failed:
        raise typer.Exit(1)
