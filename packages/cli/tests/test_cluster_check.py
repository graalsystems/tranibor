"""`tranibor cluster check` (S87-03) against a fake API server: each check's three outcomes, the
namespace always deleted, the values file read, the exit code and the JSON.

The fake serves what a real API server answers on the paths the checks read and the objects they
create; what the probe pods print is scripted. The scripts themselves run here too, under `sh`,
with a fake `nc`: the gate's loop is the egress gate's, and a test says so.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from tranibor_cli import cluster_check, kube
from tranibor_cli.cluster_check import Settings, Status
from tranibor_cli.kube import Connection, KubeApi, KubeConfigError
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[3]
OPEN_LOG = "server=open\ngate=open\n"
FENCED_CLOSED = (
    "gate=closed waited=3.54 attempts=3 open_attempts=0 open_at=\n"
    "server=closed waited=3.51 attempts=3 open_attempts=0 open_at=\n"
)
FENCED_OPEN = (
    "gate=open waited=30.02 attempts=31 open_attempts=31 open_at=0.01,1.02\n"
    "server=open waited=30.01 attempts=31 open_attempts=31 open_at=0.01\n"
)


class FakeCluster:
    """The API server's answers on the paths the checks use, and a record of what they did."""

    def __init__(self) -> None:
        self.version: dict[str, Any] = {"major": "1", "minor": "31", "gitVersion": "v1.31.4+k3s1"}
        self.apis: dict[str, list[str]] = {
            "agents.x-k8s.io/v1beta1": ["sandboxes", "sandboxes/status"],
            "extensions.agents.x-k8s.io/v1beta1": ["sandboxtemplates", "sandboxclaims", "sandboxwarmpools"],
            "monitoring.coreos.com/v1": ["servicemonitors", "prometheusrules", "podmonitors"],
        }
        self.controller_image = (
            "registry.k8s.io/agent-sandbox/agent-sandbox-controller@" + cluster_check.AGENT_SANDBOX_DIGEST
        )
        self.runtime_classes = ["gvisor"]
        self.storage_classes: dict[str, bool] = {"local-path": True}
        self.logs = {"open": OPEN_LOG, "fenced": FENCED_CLOSED}
        self.pod_phase = {"server": "Running", "open": "Succeeded", "fenced": "Succeeded"}
        self.refuse_policy = False
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            if path.endswith("/networkpolicies") and self.refuse_policy:
                return httpx.Response(422, json={"message": "NetworkPolicy is not served"})
            self.created.append((path, body))
            return httpx.Response(201, json=body)
        if request.method == "DELETE":
            self.deleted.append(path)
            return httpx.Response(200, json={})
        if path == "/version":
            return httpx.Response(200, json=self.version)
        if path.startswith("/apis/") and path.count("/") == 3:
            group_version = path.removeprefix("/apis/")
            if group_version not in self.apis:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"resources": [{"name": n} for n in self.apis[group_version]]})
        if path == "/apis/apps/v1/deployments":
            containers = [{"image": self.controller_image}] if self.controller_image else []
            items = [
                {
                    "metadata": {"namespace": "agent-sandbox-system"},
                    "spec": {"template": {"spec": {"containers": containers}}},
                }
            ]
            return httpx.Response(200, json={"items": items})
        if path.startswith("/apis/node.k8s.io/v1/runtimeclasses"):
            name = path.rsplit("/", 1)[-1]
            if name == "runtimeclasses":
                items = [{"metadata": {"name": n}} for n in self.runtime_classes]
                return httpx.Response(200, json={"items": items})
            if name in self.runtime_classes:
                return httpx.Response(200, json={"metadata": {"name": name}})
            return httpx.Response(404, json={})
        if path == "/apis/storage.k8s.io/v1/storageclasses":
            items = [
                {
                    "metadata": {
                        "name": name,
                        "annotations": {"storageclass.kubernetes.io/is-default-class": str(default).lower()},
                    }
                }
                for name, default in self.storage_classes.items()
            ]
            return httpx.Response(200, json={"items": items})
        if "/pods/" in path:
            name = path.split("/pods/", 1)[1]
            if name.endswith("/log"):
                return httpx.Response(200, text=self.logs[name.removesuffix("/log")])
            phase = self.pod_phase[name]
            status: dict[str, Any] = {"phase": phase, "podIP": "10.42.0.7"}
            if phase == "Running":
                status["conditions"] = [{"type": "Ready", "status": "True"}]
            if phase == "Pending":
                status["containerStatuses"] = [
                    {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "pull refused"}}}
                ]
            return httpx.Response(200, json={"status": status})
        return httpx.Response(404, json={})

    def api(self) -> KubeApi:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return KubeApi(Connection("http://fake", "fake", True, {}), client=client)

    def created_kinds(self) -> list[str]:
        return [str(body.get("kind")) for _, body in self.created]


async def _no_sleep(_: float) -> None:
    return None


async def run(cluster: FakeCluster, **settings: Any) -> dict[str, cluster_check.Result]:
    api = cluster.api()
    try:
        results = await cluster_check.run_checks(api, Settings(**settings), sleep=_no_sleep)
    finally:
        await api.aclose()
    return {result.id: result for result in results}


async def test_a_cluster_that_has_everything_passes_every_check() -> None:
    cluster = FakeCluster()
    results = await run(cluster, runtime_class="gvisor")
    assert {r.status for r in results.values()} == {Status.OK, Status.NOT_APPLICABLE}
    assert results["object-store"].status is Status.NOT_APPLICABLE
    assert [r.status for k, r in results.items() if k != "object-store"] == [Status.OK] * 8


async def test_a_cluster_older_than_1_29_fails_the_version_check() -> None:
    cluster = FakeCluster()
    cluster.version = {"major": "1", "minor": "28", "gitVersion": "v1.28.15"}
    result = (await run(cluster))["kubernetes-version"]
    assert result.status is Status.FAIL
    assert "1.29" in result.detail and "upgrade" in result.fix


async def test_agent_sandbox_missing_fails_and_names_the_fix() -> None:
    cluster = FakeCluster()
    del cluster.apis["extensions.agents.x-k8s.io/v1beta1"]
    cluster.controller_image = ""
    results = await run(cluster)
    assert results["agent-sandbox-crds"].status is Status.FAIL
    assert "sandboxtemplates" in results["agent-sandbox-crds"].detail
    assert "k8s_job" in results["agent-sandbox-crds"].fix
    assert results["agent-sandbox-version"].status is Status.FAIL


async def test_an_agent_sandbox_release_other_than_the_pin_fails() -> None:
    cluster = FakeCluster()
    cluster.controller_image = "registry.k8s.io/agent-sandbox/agent-sandbox-controller:v1.1.0"
    result = (await run(cluster))["agent-sandbox-version"]
    assert result.status is Status.FAIL
    assert "v1.1.0" in result.detail and cluster_check.AGENT_SANDBOX_RELEASE in result.fix


async def test_agent_sandbox_is_not_applicable_to_another_executor() -> None:
    cluster = FakeCluster()
    cluster.apis.clear()
    results = await run(cluster, executor="k8s_job", service_monitor=False, alerts=False)
    for key in ("agent-sandbox-crds", "agent-sandbox-version", "monitoring-crds"):
        assert results[key].status is Status.NOT_APPLICABLE
    assert "k8s_job" in results["agent-sandbox-crds"].detail


async def test_a_runtime_class_the_cluster_lacks_fails_and_lists_the_ones_it_has() -> None:
    cluster = FakeCluster()
    cluster.runtime_classes = ["kata", "runc"]
    result = (await run(cluster, runtime_class="gvisor"))["runtime-class"]
    assert result.status is Status.FAIL
    assert "kata, runc" in result.detail
    assert (await run(cluster))["runtime-class"].status is Status.NOT_APPLICABLE


async def test_no_default_storage_class_fails() -> None:
    cluster = FakeCluster()
    cluster.storage_classes = {"gp3": False}
    result = (await run(cluster))["default-storage-class"]
    assert result.status is Status.FAIL
    assert "gp3" in result.detail and "kubectl patch storageclass gp3" in result.fix


async def test_monitoring_crds_follow_the_flavour() -> None:
    cluster = FakeCluster()
    assert (await run(cluster))["monitoring-crds"].status is Status.OK
    result = (await run(cluster, monitoring_flavour="victoriametrics"))["monitoring-crds"]
    assert result.status is Status.FAIL
    assert "vmservicescrapes" in result.detail and "monitoring.serviceMonitor: false" in result.fix
    cluster.apis["operator.victoriametrics.com/v1beta1"] = ["vmservicescrapes", "vmrules"]
    assert (await run(cluster, monitoring_flavour="victoriametrics"))["monitoring-crds"].status is Status.OK


async def test_a_policy_accepted_but_not_enforced_fails_and_names_it() -> None:
    """The acceptance criterion of S87-03: kind without a NetworkPolicy engine accepts the policy, and the
    pod it denies still gets through."""
    cluster = FakeCluster()
    cluster.logs["fenced"] = FENCED_OPEN
    results = await run(cluster)
    enforced = results["network-policy-enforced"]
    assert enforced.status is Status.FAIL
    assert "NetworkPolicy is not enforced" in enforced.detail
    assert "Calico" in enforced.fix
    assert results["egress-gate"].status is Status.FAIL
    assert "115" in results["egress-gate"].detail
    # The policy was given to the API server before the pods, and the pods carry the runner's labels.
    kinds = cluster.created_kinds()
    assert kinds == ["Namespace", "NetworkPolicy", "Pod", "Pod", "Pod"]
    fenced = cluster.created[-1][1]
    assert fenced["metadata"]["labels"]["app.kubernetes.io/name"] == "choregos-runner"


async def test_the_server_unreachable_without_a_policy_is_inconclusive_not_a_pass() -> None:
    cluster = FakeCluster()
    cluster.logs["open"] = "server=closed\ngate=open\n"
    result = (await run(cluster))["network-policy-enforced"]
    assert result.status is Status.FAIL
    assert "inconclusive" in result.detail


async def test_a_gate_target_nobody_reaches_proves_nothing() -> None:
    cluster = FakeCluster()
    cluster.logs["open"] = "server=open\ngate=closed\n"
    result = (await run(cluster))["egress-gate"]
    assert result.status is Status.NOT_APPLICABLE
    assert "proves nothing" in result.detail


async def test_the_gate_off_is_not_applicable_and_not_probed() -> None:
    cluster = FakeCluster()
    result = (await run(cluster, gate_enabled=False))["egress-gate"]
    assert result.status is Status.NOT_APPLICABLE
    env = {e["name"]: e["value"] for e in cluster.created[-1][1]["spec"]["containers"][0]["env"]}
    assert env["GATE_HOST"] == ""


async def test_the_gate_settings_reach_the_probe() -> None:
    cluster = FakeCluster()
    await run(cluster, gate_target="192.0.2.10:8443", gate_deadline_s=12)
    env = {e["name"]: e["value"] for e in cluster.created[-1][1]["spec"]["containers"][0]["env"]}
    assert (env["GATE_HOST"], env["GATE_PORT"], env["DEADLINE"]) == ("192.0.2.10", "8443", "12")
    assert (env["STREAK"], env["TIMEOUT"], env["INTERVAL"]) == ("3", "1", "0.25")


async def test_the_object_store_is_probed_only_given_an_endpoint() -> None:
    cluster = FakeCluster()
    cluster.logs["open"] = OPEN_LOG + "store=open\n"
    result = (await run(cluster, object_store_endpoint="http://rustfs.storage:9000"))["object-store"]
    assert result.status is Status.OK
    env = {e["name"]: e["value"] for e in cluster.created[-2][1]["spec"]["containers"][0]["env"]}
    assert (env["STORE_HOST"], env["STORE_PORT"]) == ("rustfs.storage", "9000")
    cluster.logs["open"] = OPEN_LOG + "store=closed\n"
    result = (await run(cluster, object_store_endpoint="https://s3.example.eu"))["object-store"]
    assert result.status is Status.FAIL


async def test_the_namespace_is_deleted_when_a_probe_pod_cannot_start() -> None:
    cluster = FakeCluster()
    cluster.pod_phase["server"] = "Pending"
    results = await run(cluster, pod_timeout_s=0)
    assert results["network-policy-enforced"].status is Status.FAIL
    assert "ImagePullBackOff" in results["network-policy-enforced"].detail
    assert "--probe-image" in results["network-policy-enforced"].fix
    namespace = cluster.created[0][1]["metadata"]["name"]
    assert namespace.startswith("tranibor-preflight-")
    assert cluster.deleted == [f"/api/v1/namespaces/{namespace}"]


async def test_a_refused_policy_fails_and_the_namespace_is_deleted() -> None:
    cluster = FakeCluster()
    cluster.refuse_policy = True
    results = await run(cluster)
    assert results["network-policy-enforced"].status is Status.FAIL
    assert "refused the policy" in results["network-policy-enforced"].detail
    assert len(cluster.deleted) == 1
    assert cluster.created_kinds() == ["Namespace"]


async def test_read_only_starts_no_pod() -> None:
    cluster = FakeCluster()
    results = await run(cluster, read_only=True)
    assert cluster.created == []
    assert results["network-policy-enforced"].status is Status.NOT_APPLICABLE


def test_the_probe_pods_are_admitted_under_the_restricted_level() -> None:
    pod = cluster_check._pod("p", {}, "img", ["sh"], {}, 8080)
    spec = pod["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    container = spec["containers"][0]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["readinessProbe"]["tcpSocket"]["port"] == 8080


def test_the_pin_is_the_one_infra_installs() -> None:
    kustomization = (ROOT / "infra/operators/agent-sandbox/kustomization.yaml").read_text(encoding="utf-8")
    assert f"ref={cluster_check.AGENT_SANDBOX_RELEASE}" in kustomization
    assert cluster_check.AGENT_SANDBOX_DIGEST in kustomization


def test_the_runner_labels_are_the_executors() -> None:
    from tranibor_adapters.executor.k8s_job import RUNNER_POD_LABELS

    assert cluster_check.RUNNER_POD_LABELS == RUNNER_POD_LABELS


# --- The probe's shell loop is the egress gate's ---------------------------------------------------


def _run_script(tmp_path: Path, script: str, nc_exit: int, **env: str) -> str:
    if shutil.which("sh") is None or shutil.which("awk") is None or not Path("/proc/uptime").exists():
        pytest.skip("needs sh, awk and /proc/uptime")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_nc = bin_dir / "nc"
    fake_nc.write_text(f"#!/bin/sh\nexit {nc_exit}\n", encoding="utf-8")
    fake_nc.chmod(0o755)
    base = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "TIMEOUT": "1", "INTERVAL": "0"}
    base |= {"STREAK": "3", "DEADLINE": "1", "SERVER_HOST": "10.0.0.1", "SERVER_PORT": "8080"}
    result = subprocess.run(
        ["sh", "-c", script], env=base | env, capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout


def test_the_fenced_probe_reports_closed_after_three_failed_attempts(tmp_path: Path) -> None:
    out = cluster_check.parse_report(
        _run_script(tmp_path, cluster_check.FENCED_SCRIPT, 1, GATE_HOST="1.1.1.1", GATE_PORT="443")
    )
    assert out["gate"]["outcome"] == "closed" and out["gate"]["attempts"] == "3"
    assert out["server"]["outcome"] == "closed"


def test_the_fenced_probe_reports_open_at_the_deadline(tmp_path: Path) -> None:
    out = cluster_check.parse_report(
        _run_script(tmp_path, cluster_check.FENCED_SCRIPT, 0, GATE_HOST="1.1.1.1", GATE_PORT="443")
    )
    assert out["gate"]["outcome"] == "open" and int(out["gate"]["open_attempts"]) >= 1
    assert out["server"]["outcome"] == "open"


def test_the_open_probe_reports_each_destination(tmp_path: Path) -> None:
    out = cluster_check.parse_report(
        _run_script(
            tmp_path, cluster_check.OPEN_SCRIPT, 0, GATE_HOST="1.1.1.1", GATE_PORT="443", STORE_HOST="s3"
        )
    )
    assert {key: value["outcome"] for key, value in out.items()} == {
        "server": "open",
        "gate": "open",
        "store": "open",
    }


def test_the_gate_loop_keeps_the_gates_constants() -> None:
    """The probe's streak, timeout and interval are the egress gate's own defaults, not copies."""
    from tranibor_core import egress_gate

    script = cluster_check.FENCED_SCRIPT
    assert 'probe "$1" "$2"' in script and '"$streak" -ge "$STREAK"' in script
    assert (egress_gate.DEFAULT_CLOSED_STREAK, egress_gate.DEFAULT_TIMEOUT_S) == (3, 1.0)


# --- The values file and the command --------------------------------------------------------------


def test_the_values_files_are_read_as_helm_merges_them(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text(
        yaml.safe_dump(
            {
                "global": {
                    "runner": {"executor": "agent_sandbox", "runtimeClass": "gvisor"},
                    "objectStore": {"endpoint": "http://rustfs:9000", "embedded": False},
                },
                "monitoring": {"flavour": "victoriametrics", "alerts": False},
                "choregos-orchestrator": {
                    "runner": {
                        "egressGate": {"enabled": True, "target": "192.0.2.1:443", "deadlineSeconds": 20}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    second = tmp_path / "b.yaml"
    second.write_text(yaml.safe_dump({"global": {"runner": {"executor": "k8s_job"}}}), encoding="utf-8")
    found = cluster_check.settings_from_values([first, second])
    assert found == {
        "executor": "k8s_job",
        "runtime_class": "gvisor",
        "monitoring_flavour": "victoriametrics",
        "alerts": False,
        "object_store_endpoint": "http://rustfs:9000",
        "gate_enabled": True,
        "gate_target": "192.0.2.1:443",
        "gate_deadline_s": 20.0,
    }


def test_an_embedded_object_store_is_not_probed(tmp_path: Path) -> None:
    values = tmp_path / "v.yaml"
    values.write_text(yaml.safe_dump({"global": {"objectStore": {"endpoint": "x", "embedded": True}}}))
    assert "object_store_endpoint" not in cluster_check.settings_from_values([values])


@pytest.fixture
def fake_cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    cluster = FakeCluster()

    async def _connection(*_: Any) -> Connection:
        return Connection("http://fake", "kind-fake", True, {})

    monkeypatch.setattr(kube, "connection", _connection)
    monkeypatch.setattr(cluster_check, "KubeApi", lambda _conn: cluster.api())
    return cluster


def test_the_command_exits_1_on_a_failure_and_prints_why_and_the_fix(fake_cluster: FakeCluster) -> None:
    from tranibor_cli.main import app

    fake_cluster.logs["fenced"] = FENCED_OPEN
    result = CliRunner().invoke(app, ["cluster", "check"])
    assert result.exit_code == 1, result.output
    assert "NetworkPolicy is not enforced" in result.output
    assert "why:" in result.output and "fix:" in result.output


def test_the_command_exits_0_and_prints_json(fake_cluster: FakeCluster, tmp_path: Path) -> None:
    from tranibor_cli.main import app

    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"global": {"runner": {"executor": "k8s_job"}}}), encoding="utf-8")
    result = CliRunner().invoke(app, ["cluster", "check", "--values", str(values), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True and report["context"] == "kind-fake"
    statuses = {check["id"]: check["status"] for check in report["checks"]}
    assert statuses["agent-sandbox-crds"] == "not applicable"
    assert statuses["network-policy-enforced"] == "ok"


def test_a_flag_overrides_the_values_file(fake_cluster: FakeCluster, tmp_path: Path) -> None:
    from tranibor_cli.main import app

    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"global": {"runner": {"executor": "k8s_job"}}}), encoding="utf-8")
    fake_cluster.apis.pop("agents.x-k8s.io/v1beta1")
    result = CliRunner().invoke(app, ["cluster", "check", "-f", str(values), "--executor", "agent_sandbox"])
    assert result.exit_code == 1
    assert "agents.x-k8s.io/v1beta1 sandboxes" in result.output


def test_an_invalid_gate_target_is_refused_before_any_call(fake_cluster: FakeCluster) -> None:
    from tranibor_cli.main import app

    result = CliRunner().invoke(app, ["cluster", "check", "--gate-target", "nowhere"])
    assert result.exit_code == 2
    assert fake_cluster.created == []


# --- The kubeconfig ---------------------------------------------------------------------------------


def _kubeconfig(tmp_path: Path, user: dict[str, Any], name: str = "config", **extra: Any) -> Path:
    path = tmp_path / name
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "dev",
        "clusters": [
            {"name": "c", "cluster": {"server": "https://10.0.0.1:6443/", "insecure-skip-tls-verify": True}}
        ],
        "contexts": [{"name": "dev", "context": {"cluster": "c", "user": "u"}}],
        "users": [{"name": "u", "user": user}],
        **extra,
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


async def test_a_token_and_the_current_context(tmp_path: Path) -> None:
    conn = await kube.connection(str(_kubeconfig(tmp_path, {"token": "t0k"})))
    assert (conn.server, conn.context) == ("https://10.0.0.1:6443", "dev")
    assert conn.headers == {"Authorization": "Bearer t0k"}


async def test_a_token_file_relative_to_the_kubeconfig(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("from-file\n", encoding="utf-8")
    conn = await kube.connection(str(_kubeconfig(tmp_path, {"tokenFile": "token"})))
    assert conn.headers == {"Authorization": "Bearer from-file"}


async def test_basic_credentials(tmp_path: Path) -> None:
    conn = await kube.connection(str(_kubeconfig(tmp_path, {"username": "a", "password": "b"})))
    assert conn.headers == {"Authorization": "Basic " + base64.b64encode(b"a:b").decode()}


async def test_an_exec_credential_plugin(tmp_path: Path) -> None:
    printed = json.dumps({"kind": "ExecCredential", "status": {"token": "from-plugin"}})
    plugin = {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "command": sys.executable,
        "args": ["-c", f"print({printed!r})"],
    }
    conn = await kube.connection(str(_kubeconfig(tmp_path, {"exec": plugin})))
    assert conn.headers == {"Authorization": "Bearer from-plugin"}


async def test_a_failing_plugin_is_said(tmp_path: Path) -> None:
    plugin = {"command": sys.executable, "args": ["-c", "import sys; sys.exit('expired')"]}
    with pytest.raises(KubeConfigError, match="exited 1: expired"):
        await kube.connection(str(_kubeconfig(tmp_path, {"exec": plugin})))


async def test_a_client_certificate_inline(tmp_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubernetes-admin")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    user = {
        "client-certificate-data": base64.b64encode(cert_pem).decode(),
        "client-key-data": base64.b64encode(key_pem).decode(),
    }
    conn = await kube.connection(str(_kubeconfig(tmp_path, user)))
    assert conn.headers == {}
    assert not isinstance(conn.verify, bool)


async def test_kubeconfig_files_merge_first_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _kubeconfig(tmp_path, {"token": "first"}, name="one")
    second = _kubeconfig(tmp_path, {"token": "second"}, name="two")
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join([str(first), str(second)]))
    conn = await kube.connection()
    assert conn.headers == {"Authorization": "Bearer first"}


async def test_an_unknown_context_names_the_known_ones(tmp_path: Path) -> None:
    with pytest.raises(KubeConfigError, match=r"no context `prod`.*it has: dev"):
        await kube.connection(str(_kubeconfig(tmp_path, {"token": "t"})), context="prod")


async def test_a_legacy_auth_provider_is_refused_with_the_way_round(tmp_path: Path) -> None:
    with pytest.raises(KubeConfigError, match="kubectl proxy"):
        await kube.connection(str(_kubeconfig(tmp_path, {"auth-provider": {"name": "gcp"}})))


async def test_server_bypasses_the_kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "absent"))
    conn = await kube.connection(server="http://127.0.0.1:8001/")
    assert conn.server == "http://127.0.0.1:8001" and conn.headers == {}
    with pytest.raises(KubeConfigError, match="no kubeconfig found"):
        await kube.connection()
