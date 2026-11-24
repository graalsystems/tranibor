"""The embedded Temporal, as an operator's deployment uses it (S80-14, ADR 0195).

An operator's repository keeps its values and nothing else: the Temporal it runs is this subchart. Four
things must hold for it to replace a hand-written manifest set: the Service name and the volume it
already has are kept, the TLS chain renders from values, `dev` puts the relay in front of the plain
frontend, and `server` runs `auto-setup` on PostgreSQL and mounts no data volume.
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

T = "choregos-temporal"
EMBEDDED = ("global.temporal.embedded=true",)
TLS_BOTH = (
    f"{T}.tls.enabled=true",
    f"{T}.tls.certManager.enabled=true",
    "global.temporal.tls.enabled=true",
    "global.temporal.tls.secretName=temporal-frontend-client-tls",
)


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "c", str(CHART), "-f", str(CHART / "values" / "local.yaml")]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=120)


def _docs(*sets: str) -> list[dict[str, Any]]:
    out = _render(*EMBEDDED, *sets)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def _temporal(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _one(docs, "Deployment", "c-temporal")["spec"]["template"]["spec"]


def test_defaults_are_unchanged_the_service_and_the_claim_carry_the_release_name() -> None:
    docs = _docs()
    assert _one(docs, "Service", "c-temporal")
    assert _one(docs, "PersistentVolumeClaim", "c-temporal")
    assert next(v for v in _temporal(docs)["volumes"] if v["name"] == "data")["persistentVolumeClaim"] == {
        "claimName": "c-temporal"
    }
    assert not [d for d in docs if d["kind"] == "Certificate"]


def test_an_existing_claim_and_service_name_are_adopted_and_no_claim_is_rendered() -> None:
    docs = _docs(
        "global.temporal.serviceName=temporal-frontend", f"{T}.persistence.existingClaim=temporal-data"
    )
    assert _one(docs, "Service", "temporal-frontend")
    assert not [
        d for d in docs if d["kind"] == "PersistentVolumeClaim" and "temporal" in d["metadata"]["name"]
    ]
    (data,) = [v for v in _temporal(docs)["volumes"] if v["name"] == "data"]
    assert data["persistentVolumeClaim"]["claimName"] == "temporal-data"


def test_the_clients_dial_the_adopted_service_name() -> None:
    docs = _docs("global.temporal.serviceName=temporal-frontend")
    api = _one(docs, "Deployment", "choregos-api")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in api["env"]}
    assert env["CHOREGOS_TEMPORAL_ADDRESS"] == "temporal-frontend:7233"


def test_dev_with_tls_puts_the_relay_on_7233_and_keeps_start_dev_on_loopback() -> None:
    docs = _docs(*TLS_BOTH, "global.temporal.serviceName=temporal-frontend")
    spec = _temporal(docs)
    main, relay = spec["containers"]
    assert main["args"][main["args"].index("--ip") + 1] == "127.0.0.1"
    assert main["args"][main["args"].index("--port") + 1] == "7234"
    assert {p["containerPort"] for p in relay["ports"]} == {7233}
    assert not [p for p in main["ports"] if p["containerPort"] == 7233]
    conf = _one(docs, "ConfigMap", "c-temporal-tls-relay")["data"]["nginx.conf"]
    assert "proxy_pass 127.0.0.1:7234" in conf and "ssl_alpn h2" in conf
    (tls,) = [v for v in spec["volumes"] if v["name"] == "tls"]
    assert tls["secret"]["secretName"] == "temporal-frontend-tls"
    template = _one(docs, "Deployment", "c-temporal")["spec"]["template"]
    assert "checksum/relay" in template["metadata"]["annotations"]


def test_the_certificate_chain_renders_and_names_the_service_and_the_namespace() -> None:
    docs = _docs(
        *TLS_BOTH,
        "global.temporal.serviceName=temporal-frontend",
        f"{T}.tls.certManager.namePrefix=temporal",
    )
    assert _one(docs, "Issuer", "temporal-selfsigned") and _one(docs, "Issuer", "temporal-ca")
    server = _one(docs, "Certificate", "temporal-frontend-tls")["spec"]
    assert "temporal-frontend" in server["dnsNames"]
    assert any(n.startswith("temporal-frontend.") and n.endswith(".svc") for n in server["dnsNames"])
    assert "client auth" in server["usages"]
    client = _one(docs, "Certificate", "temporal-frontend-client-tls")["spec"]
    assert client["secretName"] == "temporal-frontend-client-tls"


def test_the_client_secret_name_can_be_pinned_to_the_one_clients_already_mount() -> None:
    docs = _docs(
        *TLS_BOTH,
        f"{T}.tls.clientSecretName=temporal-client-tls",
        "global.temporal.tls.secretName=temporal-client-tls",
    )
    assert _one(docs, "Certificate", "temporal-client-tls")


def test_a_tls_server_needs_tls_clients() -> None:
    out = _render(*EMBEDDED, f"{T}.tls.enabled=true")
    assert out.returncode != 0 and "global.temporal.tls.enabled" in out.stderr


def test_dev_tls_without_the_ui_on_the_network_probes_the_frontend_from_inside() -> None:
    # Frontend on loopback behind the relay, UI off or on loopback: only the relay's port answers.
    for sets in ((f"{T}.ui.enabled=false",), (f"{T}.ui.loopbackOnly=true",)):
        main = _temporal(_docs(*TLS_BOTH, *sets))["containers"][0]
        command = main["readinessProbe"]["exec"]["command"]
        assert command[:4] == ["temporal", "operator", "cluster", "health"], sets
        assert command[command.index("--address") + 1] == "127.0.0.1:7234", sets
    default = _temporal(_docs(*TLS_BOTH))["containers"][0]
    assert default["readinessProbe"]["tcpSocket"] == {"port": "ui"}


def test_a_loopback_ui_leaves_the_pod_network_and_the_service() -> None:
    docs = _docs(*TLS_BOTH, f"{T}.ui.loopbackOnly=true")
    main = _temporal(docs)["containers"][0]
    assert main["args"][main["args"].index("--ui-ip") + 1] == "127.0.0.1"
    assert not [p for p in main.get("ports") or [] if p["containerPort"] == 8233]
    assert {p["port"] for p in _one(docs, "Service", "c-temporal")["spec"]["ports"]} == {7233}
    # Without TLS too: start-dev binds 0.0.0.0 then, and the UI would follow `--ip`.
    plain = _temporal(_docs(f"{T}.ui.loopbackOnly=true"))["containers"][0]
    assert plain["args"][plain["args"].index("--ui-ip") + 1] == "127.0.0.1"
    # Default: the UI stays where it was.
    service = _one(_docs(*TLS_BOTH), "Service", "c-temporal")
    assert {p["port"] for p in service["spec"]["ports"]} == {7233, 8233}


MTLS = (*TLS_BOTH, f"{T}.tls.requireClientCert=true", "global.temporal.tls.mutual=true")


def test_dev_mtls_the_relay_cuts_a_client_without_a_certificate_from_the_authority() -> None:
    conf = _one(_docs(*MTLS), "ConfigMap", "c-temporal-tls-relay")["data"]["nginx.conf"]
    assert "ssl_verify_client on;" in conf
    assert "ssl_client_certificate /etc/temporal-tls/ca.crt;" in conf
    plain = _one(_docs(*TLS_BOTH), "ConfigMap", "c-temporal-tls-relay")["data"]["nginx.conf"]
    assert "ssl_verify_client" not in plain


def test_mtls_needs_clients_that_present_a_certificate_and_a_tls_server() -> None:
    no_cert = _render(*EMBEDDED, *TLS_BOTH, f"{T}.tls.requireClientCert=true")
    assert no_cert.returncode != 0 and "global.temporal.tls.mutual" in no_cert.stderr
    no_tls = _render(*EMBEDDED, f"{T}.tls.requireClientCert=true", "global.temporal.tls.mutual=true")
    assert no_tls.returncode != 0 and "choregos-temporal.tls.enabled" in no_tls.stderr


def test_an_unknown_mode_is_refused() -> None:
    out = _render(*EMBEDDED, f"{T}.mode=cluster")
    assert out.returncode != 0 and "mode=cluster" in out.stderr


SERVER = (
    f"{T}.mode=server",
    f"{T}.database.host=choregos-pg",
    f"{T}.database.user=temporal",
    f"{T}.database.passwordSecret.name=temporal.choregos-pg.credentials",
    f"{T}.database.skipCreate=true",
    f"{T}.database.tls.enabled=true",
)


def test_server_mode_runs_auto_setup_on_postgresql_and_mounts_no_data_volume() -> None:
    docs = _docs(*SERVER)
    spec = _temporal(docs)
    (main,) = spec["containers"]
    assert main["image"].startswith("temporalio/auto-setup:")
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["POSTGRES_SEEDS"] == "choregos-pg" and env["POSTGRES_USER"] == "temporal"
    assert env["SKIP_DB_CREATE"] == "true" and env["DBNAME"] == "temporal"
    assert env["VISIBILITY_DBNAME"] == "temporal_visibility"
    (pwd,) = [e for e in main["env"] if e["name"] == "POSTGRES_PWD"]
    assert pwd["valueFrom"]["secretKeyRef"] == {"name": "temporal.choregos-pg.credentials", "key": "password"}
    # The schema tool reads other variables than the server: both pairs, or it connects in clear.
    assert env["SQL_TLS_ENABLED"] == "true" and env["POSTGRES_TLS_ENABLED"] == "true"
    assert not [v for v in spec.get("volumes", []) if v["name"] == "data"]
    assert not [
        d for d in docs if d["kind"] == "PersistentVolumeClaim" and "temporal" in d["metadata"]["name"]
    ]


def test_server_mode_serves_tls_itself_with_no_relay() -> None:
    docs = _docs(*SERVER, *TLS_BOTH, "global.temporal.serviceName=temporal-frontend")
    spec = _temporal(docs)
    (main,) = spec["containers"]
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["TEMPORAL_TLS_SERVER_CERT"] == "/etc/temporal-tls/tls.crt"
    assert env["TEMPORAL_TLS_FRONTEND_SERVER_NAME"] == "temporal-frontend"
    assert env["TEMPORAL_TLS_SERVER_NAME"] == "temporal-frontend"
    assert {p["containerPort"] for p in main["ports"]} == {7233}
    assert not [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("tls-relay")]


def test_server_mode_mtls_the_frontend_and_the_internal_services_require_a_client_certificate() -> None:
    (main,) = _temporal(_docs(*SERVER, *MTLS))["containers"]
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["TEMPORAL_TLS_REQUIRE_CLIENT_AUTH"] == "true"
    # Both entries: the image's template lists CLIENT2 whenever CLIENT1 is set.
    assert env["TEMPORAL_TLS_CLIENT1_CA_CERT"] == "/etc/temporal-tls/ca.crt"
    assert env["TEMPORAL_TLS_CLIENT2_CA_CERT"] == "/etc/temporal-tls/ca.crt"
    # The bootstrap script's CLI (health, the default namespace) presents the server certificate.
    assert env["TEMPORAL_TLS_CERT"] == "/etc/temporal-tls/tls.crt"
    assert env["TEMPORAL_TLS_KEY"] == "/etc/temporal-tls/tls.key"
    (plain,) = _temporal(_docs(*SERVER, *TLS_BOTH))["containers"]
    names = {e["name"] for e in plain["env"]}
    assert not names & {"TEMPORAL_TLS_REQUIRE_CLIENT_AUTH", "TEMPORAL_TLS_CERT"}


def test_server_mode_is_the_embedded_temporal_a_serious_environment_may_use() -> None:
    base = (
        "global.environment=staging",
        "global.database.embedded=false",
        "global.database.host=pg",
        "global.gateway.embedded=false",
        "global.memory.embedded=false",
        "global.devSecrets=false",
        "global.devLogin.enabled=false",
    )
    refused = _render(*EMBEDDED, *base)
    assert refused.returncode != 0 and "choregos-temporal.mode=dev" in refused.stderr
    assert _render(*EMBEDDED, *base, f"{T}.mode=server").returncode == 0, "server mode is allowed"
