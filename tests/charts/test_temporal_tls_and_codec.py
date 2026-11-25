"""Temporal over TLS and its payload codec reach the API and every worker (S80-06, ADR 0183, ADR 0220).

The chart mounts the TLS Secret and the codec Secret into the pods that hold a Temporal client, and
sets the variables `tranibor_core.temporal_connection` reads. TLS is off by default. The codec is ON by
default: the chart draws a master key into a Secret when none is named, and refuses a staging or prod
release that turns it off without saying `allowClear`.
"""

from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

from .conftest import both_paths

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")

ON = (
    "global.temporal.tls.enabled=true",
    "global.temporal.tls.secretName=temporal-client",
    "global.temporal.tls.mutual=true",
    "global.temporal.tls.serverName=frontend.temporal.internal",
    "global.temporal.codec.enabled=true",
    "global.temporal.codec.secretName=temporal-codec",
)


def _render(*sets: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    args = ["helm", "template", "choregos", str(CHART), "-f", str(CHART / "values" / "local.yaml")]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=120)


def _temporal_clients(*sets: str) -> dict[str, dict[str, Any]]:
    """Each Deployment holding a Temporal client (the API, one per worker queue), by name."""
    docs = [d for d in yaml.safe_load_all(_render(*sets).stdout) if d]
    pods: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        if name == "tranibor-api" or name.startswith("tranibor-orchestrator-"):
            pods[name] = doc["spec"]["template"]["spec"]
    return pods


def _env(spec: dict[str, Any]) -> dict[str, str]:
    (container,) = [c for c in spec["containers"] if c["name"] in ("api", "worker")]
    return {v["name"]: v.get("value", "") for v in container.get("env", [])}


def _mounts(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Each volume's mount paths: the chart's `/etc/tranibor/…`, then its former `/etc/choregos/…` (R9b)."""
    (container,) = [c for c in spec["containers"] if c["name"] in ("api", "worker")]
    found: dict[str, list[str]] = {}
    for mount in container.get("volumeMounts", []):
        found.setdefault(mount["name"], []).append(mount["mountPath"])
    return found


def _secret_volumes(spec: dict[str, Any]) -> dict[str, str]:
    return {v["name"]: v["secret"]["secretName"] for v in spec.get("volumes", []) if "secret" in v}


def test_tls_is_off_by_default_and_the_codec_is_on() -> None:
    pods = _temporal_clients()
    assert "tranibor-api" in pods and any(n.startswith("tranibor-orchestrator-") for n in pods)
    for name, spec in pods.items():
        env = _env(spec)
        assert not [k for k in env if k.startswith("TRANIBOR_TEMPORAL_TLS")], name
        assert env["TRANIBOR_TEMPORAL_CODEC_KEYS_FILE"] == "/etc/tranibor/temporal-codec/keys", name
        assert "TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS" not in env, name
        assert "temporal-tls" not in _mounts(spec), name
        assert _mounts(spec)["temporal-codec"] == both_paths("/etc/tranibor/temporal-codec"), name
        assert _secret_volumes(spec)["temporal-codec"] == "choregos-temporal-codec", name


def test_the_chart_draws_the_master_key_into_the_secret_the_pods_mount() -> None:
    secrets = [
        d
        for d in yaml.safe_load_all(_render().stdout)
        if d and d.get("kind") == "Secret" and d["metadata"]["name"] == "choregos-temporal-codec"
    ]
    assert len(secrets) == 1
    (secret,) = secrets
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    key_id, _, encoded = secret["stringData"]["keys"].partition(":")
    assert key_id == "v1" and len(base64.b64decode(encoded, validate=True)) == 32
    # the generated ring is what the process parses
    from tranibor_core.temporal_connection import parse_codec_keys

    assert [k for k, _ in parse_codec_keys(secret["stringData"]["keys"])] == ["v1"]


def test_a_secret_named_by_the_operator_is_mounted_and_none_is_drawn() -> None:
    sets = ("global.temporal.codec.secretName=my-keys",)
    docs = [d for d in yaml.safe_load_all(_render(*sets).stdout) if d]
    assert not [
        d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("temporal-codec")
    ]
    for name, spec in _temporal_clients(*sets).items():
        assert _secret_volumes(spec)["temporal-codec"] == "my-keys", name


def test_the_opt_out_in_dev_mounts_nothing_and_sets_no_variable() -> None:
    for name, spec in _temporal_clients("global.temporal.codec.enabled=false").items():
        assert not [k for k in _env(spec) if k.startswith("TRANIBOR_TEMPORAL_CODEC")], name
        assert "temporal-codec" not in _mounts(spec), name


def _render_prod(*sets: str) -> subprocess.CompletedProcess[str]:
    prod = ("global.environment=prod", "global.database.embedded=false", "global.temporal.embedded=false")
    args = ["helm", "template", "choregos", str(CHART), "-f", str(CHART / "values" / "prod.yaml")]
    for s in (*prod, *sets):
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=120)


def test_production_with_the_codec_on_renders() -> None:
    assert _render_prod().returncode == 0


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_production_without_the_codec_is_refused_unless_allow_clear_says_so(environment: str) -> None:
    off = ("global.temporal.codec.enabled=false", f"global.environment={environment}")
    refused = _render_prod(*off)
    assert refused.returncode != 0 and "global.temporal.codec.allowClear" in refused.stderr
    allowed = _render_prod(*off, "global.temporal.codec.allowClear=true")
    assert allowed.returncode == 0, allowed.stderr
    envs = [
        c["env"]
        for d in yaml.safe_load_all(allowed.stdout)
        if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "tranibor-api"
        for c in d["spec"]["template"]["spec"]["containers"]
        if c["name"] == "api"
    ]
    (api_env,) = envs
    values = {e["name"]: e.get("value") for e in api_env}
    assert values["TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS"] == "true"
    assert "TRANIBOR_TEMPORAL_CODEC_KEYS_FILE" not in values


def test_enabled_the_api_and_every_worker_mount_both_secrets_and_name_the_files() -> None:
    pods = _temporal_clients(*ON)
    assert len(pods) >= 2
    for name, spec in pods.items():
        assert (
            _secret_volumes(spec).items()
            >= {
                "temporal-tls": "temporal-client",
                "temporal-codec": "temporal-codec",
            }.items()
        ), name
        mounts = _mounts(spec)
        assert mounts["temporal-tls"] == both_paths("/etc/tranibor/temporal-tls"), name
        assert mounts["temporal-codec"] == both_paths("/etc/tranibor/temporal-codec"), name
        env = _env(spec)
        assert env["TRANIBOR_TEMPORAL_TLS"] == "true", name
        assert env["TRANIBOR_TEMPORAL_TLS_CA_FILE"] == "/etc/tranibor/temporal-tls/ca.crt", name
        assert env["TRANIBOR_TEMPORAL_TLS_CERT_FILE"] == "/etc/tranibor/temporal-tls/tls.crt", name
        assert env["TRANIBOR_TEMPORAL_TLS_KEY_FILE"] == "/etc/tranibor/temporal-tls/tls.key", name
        assert env["TRANIBOR_TEMPORAL_TLS_SERVER_NAME"] == "frontend.temporal.internal", name
        assert env["TRANIBOR_TEMPORAL_CODEC_KEYS_FILE"] == "/etc/tranibor/temporal-codec/keys", name


def test_without_mutual_tls_no_client_certificate_is_named() -> None:
    for spec in _temporal_clients(
        "global.temporal.tls.enabled=true", "global.temporal.tls.secretName=ca"
    ).values():
        env = _env(spec)
        assert env["TRANIBOR_TEMPORAL_TLS_CA_FILE"].endswith("/ca.crt")
        assert "TRANIBOR_TEMPORAL_TLS_CERT_FILE" not in env and "TRANIBOR_TEMPORAL_TLS_KEY_FILE" not in env
        assert _mounts(spec)["temporal-codec"] == both_paths("/etc/tranibor/temporal-codec")


@pytest.mark.parametrize(
    ("sets", "named"),
    [
        (("global.temporal.tls.enabled=true",), "global.temporal.tls.secretName"),
        (
            (*ON, "global.extraEnv[0].name=TRANIBOR_TEMPORAL_CODEC_KEYS", "global.extraEnv[0].value=x"),
            "TRANIBOR_TEMPORAL_CODEC_KEYS",
        ),
    ],
)
def test_a_tls_switch_without_its_secret_or_a_second_value_is_refused(
    sets: tuple[str, ...], named: str
) -> None:
    refused = _render(*sets, check=False)
    assert refused.returncode != 0
    assert named in refused.stderr
