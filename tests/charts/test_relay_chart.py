"""The chart `choregos-relay`: a relay anyone can run (S87-44, ADR 0230 §12).

The relay routes a phone's TLS by SNI to the installation that dialled out to it, and never
terminates that TLS (ADR 0230 §4). Its chart deploys it as it is: one process with two listeners, the
SNI listener behind a Service on 443 that nothing in front terminates, and the relay's own host
(pairing, control, dial-backs) behind a Service or an Ingress of its own. The relay's own material,
its signing key and its database, comes from a Secret the operator created and names: no value of
the chart holds a secret, and the chart renders no Secret.

Each test renders the chart with `helm template` and reads the objects. Where the chart writes what
the relay reads, the relay's own code reads it back (`tranibor_sni_relay.server.relay_config`).
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "choregos-relay"
EXAMPLE = CHART / "values" / "example.yaml"
DOMAIN = "relay.example.com"
#: What a deployment must give: the domain, the retention, its Secret and the image it built.
MINIMAL = (
    f"relay.domain={DOMAIN}",
    "relay.retentionDays=30",
    "existingSecret=relay-own",
    "image.registry=registry.example.com",
    "image.repository=choregos-relay",
)
#: Where the relay writes its metadata lines and its scratch files: the only writable places.
METADATA_DIR, TMP = "/var/lib/choregos-relay", "/tmp"
#: Values set as strings: annotations.
STRINGS = ("passthrough.annotations.", "own.service.annotations.", "own.ingress.annotations.")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*sets: str, values: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "relay", str(CHART), "--namespace", "relay"]
    if values is not None:
        command += ["-f", str(values)]
    for value in sets:
        # An annotation is a string, whatever it looks like (`true`, `443`, `{...}`).
        command += ["--set-string" if value.startswith(STRINGS) else "--set", value]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _documents(*sets: str, values: pathlib.Path | None = None) -> list[dict[str, Any]]:
    rendered = _render(*sets, values=values)
    assert rendered.returncode == 0, rendered.stderr[-800:]
    return [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]


def _refusal(*sets: str) -> str:
    rendered = _render(*sets)
    assert rendered.returncode != 0, "the chart rendered what it must refuse"
    return rendered.stderr


def _one(documents: list[dict[str, Any]], kind: str, suffix: str | None = None) -> dict[str, Any]:
    (found,) = [
        d
        for d in documents
        if d["kind"] == kind and (suffix is None or d["metadata"]["name"].endswith(suffix))
    ]
    return found


def _pod(documents: list[dict[str, Any]]) -> dict[str, Any]:
    pod: dict[str, Any] = _one(documents, "Deployment")["spec"]["template"]["spec"]
    return pod


def _relay(documents: list[dict[str, Any]]) -> dict[str, Any]:
    (container,) = _pod(documents)["containers"]
    assert container["name"] == "relay"
    return container


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in container.get("env") or []}


def _literal_env(container: dict[str, Any]) -> dict[str, str]:
    """The variables the chart writes as values, as the process sees them."""
    return {name: str(item["value"]) for name, item in _env(container).items() if "value" in item}


def _port(container: dict[str, Any], name: str) -> int:
    (port,) = [p["containerPort"] for p in container["ports"] if p["name"] == name]
    return int(port)


# ───────────────────────── a chart of its own ─────────────────────────


def test_it_is_a_chart_of_its_own_at_the_repositorys_version() -> None:
    """Not a subchart of the platform's and no dependency; one version everywhere, which the release
    checks before it tags (`tools/bump_version.py`)."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert chart["name"] == "choregos-relay" and "dependencies" not in chart
    platform = yaml.safe_load((ROOT / "charts/choregos/Chart.yaml").read_text(encoding="utf-8"))
    assert "choregos-relay" not in {dependency["name"] for dependency in platform["dependencies"]}
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "global" not in values, "`global` is how a parent chart reaches into a subchart"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert chart["version"] == version and chart["appVersion"] == version

    import importlib.util

    spec = importlib.util.spec_from_file_location("bump_version", ROOT / "tools" / "bump_version.py")
    assert spec and spec.loader
    bump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump)
    assert CHART / "Chart.yaml" in [path for path, _ in bump._files()], "a release would leave it behind"


def test_the_image_is_the_one_the_operator_built_pinned_by_digest_when_given() -> None:
    """No release publishes the relay's image yet: the operator builds `docker/relay.Dockerfile` and
    names it. The chart refuses to guess one."""
    container = _relay(_documents(*MINIMAL))
    version = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))["appVersion"]
    assert container["image"] == f"registry.example.com/choregos-relay:{version}"
    digest = "sha256:" + "ab" * 32
    pinned = _relay(_documents(*MINIMAL, f"image.digest={digest}", "image.tag=9.9.9"))
    assert pinned["image"] == f"registry.example.com/choregos-relay@{digest}"
    assert "image.digest" in _refusal(*MINIMAL, "image.digest=latest")
    assert "image.repository" in _refusal(*MINIMAL, "image.repository=")
    # `serve` is what the pod runs, through the image's own entrypoint.
    assert container["args"] == ["serve"] and "command" not in container


# ───────────────────────── no secret in the values ─────────────────────────

#: What a secret looks like when it is typed in a file.
SECRET_SHAPES = (
    re.compile(r"-----BEGIN [A-Z ]+-----"),  # a PEM block: a key, a certificate
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # a URL that carries its password
    re.compile(r"^[A-Za-z0-9+/_=-]{32,}$"),  # a token, a key or a password as one long word
)
#: The names under which a secret is usually typed.
SECRET_WORDS = re.compile(
    r"(?i)password|passwd|secret|token|private|credential|api_?key|dsn|database|signing|key"
)
#: Values that name a Secret without holding one: the operator's, and the TLS Secrets of the own host.
NAMES_OF_SECRETS = {
    ("existingSecret",),
    ("own", "tls", "secretName"),
    ("own", "ingress", "tls", "secretName"),
}
SECRET_KEY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
#: An annotation's key names what it configures, not a secret: its value is still read.
ANNOTATIONS = {"annotations", "podAnnotations"}


def _leaves(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaves(value, (*path, str(index)))
    else:
        yield path, node


@pytest.mark.parametrize("values", [CHART / "values.yaml", EXAMPLE], ids=lambda p: p.name)
def test_no_value_holds_a_secret(values: pathlib.Path) -> None:
    """The defaults and the example name a Secret and the keys in it, and hold nothing secret."""
    document = yaml.safe_load(values.read_text(encoding="utf-8"))
    for path, leaf in _leaves(document):
        if not isinstance(leaf, str) or not leaf:
            continue
        where = ".".join(path)
        assert not any(shape.search(leaf) for shape in SECRET_SHAPES), f"{where}: a secret in {values.name}"
        if path in NAMES_OF_SECRETS or path[:1] == ("secretKeys",):
            assert SECRET_KEY_NAME.match(leaf), f"{where}: {leaf!r} names no Secret and no key of one"
        elif not ANNOTATIONS & set(path):
            assert not SECRET_WORDS.search(path[-1]), f"{where}: a secret in {values.name}"
    defaults = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert defaults["existingSecret"] == "", "the default names no Secret: the operator names theirs"
    assert defaults["secretKeys"] == {"databaseUrl": "database-url", "signingKey": "signing-key"}


def test_the_relays_own_material_comes_from_the_operators_secret_and_the_chart_renders_none() -> None:
    documents = _documents(*MINIMAL)
    assert not [d for d in documents if d["kind"] == "Secret"], "the chart renders a Secret"
    container = _relay(documents)
    env = _env(container)
    # The database's URL: a key of the operator's Secret, never a value.
    database = env["TRANIBOR_RELAY_DATABASE_URL"]
    assert "value" not in database
    assert database["valueFrom"]["secretKeyRef"] == {"name": "relay-own", "key": "database-url"}
    # The signing key: a file from the same Secret, read-only, readable by the pod's group alone (fsGroup).
    volumes = {v["name"]: v for v in _pod(documents)["volumes"]}
    secret = volumes["secret"]["secret"]
    assert secret["secretName"] == "relay-own"
    assert secret["items"] == [{"key": "signing-key", "path": "signing-key"}]
    assert secret["defaultMode"] == 0o440
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["secret"]["readOnly"] is True
    assert env["TRANIBOR_RELAY_SIGNING_KEY_FILE"]["value"] == f"{mounts['secret']['mountPath']}/signing-key"
    # No variable that names a secret carries a value.
    for name, item in env.items():
        if SECRET_WORDS.search(name.removeprefix("TRANIBOR_RELAY_").replace("_FILE", "")):
            assert "value" not in item or item["value"].startswith("/"), name
    # The keys are the operator's to name.
    renamed = _relay(_documents(*MINIMAL, "secretKeys.databaseUrl=url", "secretKeys.signingKey=key.pem"))
    assert _env(renamed)["TRANIBOR_RELAY_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "url"
    assert "existingSecret" in _refusal(*MINIMAL, "existingSecret=")


# ───────────────────────── the passthrough Service ─────────────────────────


def test_the_passthrough_service_exposes_443_to_the_sni_listener_and_terminates_nothing() -> None:
    documents = _documents(*MINIMAL)
    service = _one(documents, "Service", "-sni")
    spec = service["spec"]
    assert spec["type"] == "LoadBalancer"
    (port,) = spec["ports"]
    assert port == {"name": "sni", "port": 443, "targetPort": "sni", "protocol": "TCP"}
    assert "appProtocol" not in port, "an application protocol invites a proxy to read the stream"
    # Whatever terminates or rewrites a stream in front of the relay is announced by an annotation:
    # the default carries none.
    assert not service["metadata"].get("annotations"), service["metadata"].get("annotations")
    # The client's address reaches the relay as the connection's peer: its metadata line says it.
    assert spec["externalTrafficPolicy"] == "Local"
    container = _relay(documents)
    labels = _one(documents, "Deployment")["spec"]["template"]["metadata"]["labels"]
    assert spec["selector"].items() <= labels.items()
    # The SNI listener is the one the relay opens without TLS, on the port the Service targets.
    from tranibor_sni_relay.server import relay_config

    config = relay_config(_literal_env(container))
    assert config.sni_port == _port(container, "sni") != _port(container, "own") == config.own_port
    # No Ingress, no route and no certificate exists for an installation's name.
    for ingress in (d for d in documents if d["kind"] == "Ingress"):
        hosts = [rule["host"] for rule in ingress["spec"]["rules"]]
        hosts += [host for tls in ingress["spec"].get("tls", []) for host in tls["hosts"]]
        assert not [host for host in hosts if host.startswith("*") or host != DOMAIN], hosts
    assert not [d for d in documents if d["kind"] in {"Certificate", "Gateway", "HTTPRoute", "TLSRoute"}]


@pytest.mark.parametrize(
    "annotation",
    [
        "service\\.beta\\.kubernetes\\.io/aws-load-balancer-ssl-cert=arn:aws:acm:eu-west-3:1:certificate/x",
        "service\\.beta\\.kubernetes\\.io/aws-load-balancer-ssl-ports=443",
        "service\\.beta\\.kubernetes\\.io/aws-load-balancer-backend-protocol=http",
        "service\\.beta\\.kubernetes\\.io/aws-load-balancer-proxy-protocol=*",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-protocol=https",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-certificate-id=abc",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-tls-ports=443",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-enable-proxy-protocol=true",
        "service\\.beta\\.kubernetes\\.io/oci-load-balancer-ssl-ports=443",
        "service\\.beta\\.kubernetes\\.io/scw-loadbalancer-certificate-ids=x",
        "service\\.beta\\.kubernetes\\.io/scw-loadbalancer-proxy-protocol-v2=true",
        "load-balancer\\.hetzner\\.cloud/http-certificates=x",
        "load-balancer\\.hetzner\\.cloud/protocol=https",
        "load-balancer\\.hetzner\\.cloud/uses-proxyprotocol=true",
        'cloud\\.google\\.com/app-protocols={"sni":"HTTPS"}',
    ],
)
def test_an_annotation_that_terminates_tls_in_front_of_the_relay_fails_the_render(annotation: str) -> None:
    """A load balancer that terminates TLS would hold the installations' traffic in clear; one that
    prepends a PROXY header hands the relay a stream that is not a ClientHello."""
    refusal = _refusal(*MINIMAL, f"passthrough.annotations.{annotation}")
    assert "passthrough.annotations" in refusal and annotation.split("=", 1)[0].replace("\\", "") in refusal


@pytest.mark.parametrize(
    "annotation",
    [
        "service\\.beta\\.kubernetes\\.io/aws-load-balancer-type=nlb",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-tls-passthrough=true",
        "service\\.beta\\.kubernetes\\.io/do-loadbalancer-protocol=tcp",
        "external-dns\\.alpha\\.kubernetes\\.io/hostname=*.relay.example.com",
    ],
)
def test_an_annotation_that_only_places_the_load_balancer_is_kept(annotation: str) -> None:
    key, value = annotation.replace("\\", "").split("=", 1)
    service = _one(_documents(*MINIMAL, f"passthrough.annotations.{annotation}"), "Service", "-sni")
    assert service["metadata"]["annotations"] == {key: value}


# ───────────────────────── the relay's own host ─────────────────────────


def test_the_own_host_has_its_own_service_and_an_ingress_for_its_paths_only() -> None:
    documents = _documents(values=EXAMPLE)
    container = _relay(documents)
    own = _one(documents, "Service", "-own")
    (port,) = own["spec"]["ports"]
    assert port["targetPort"] == "own" and own["spec"]["type"] == "ClusterIP"
    ingress = _one(documents, "Ingress")
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == DOMAIN, "the own host is the domain itself unless named"
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/relay/v1" and path["pathType"] == "Prefix"
    assert path["backend"]["service"] == {"name": own["metadata"]["name"], "port": {"name": "own"}}
    # The agents dial the own host where the Ingress publishes it, and sign that URL.
    from tranibor_sni_relay.server import relay_config

    config = relay_config(_literal_env(container))
    assert config.own_public_port == 443 and config.own_tls is None
    assert config.own_host in (None, DOMAIN)


def test_the_relay_may_terminate_its_own_hosts_tls_itself_and_then_no_ingress_does() -> None:
    documents = _documents(*MINIMAL, "own.service.type=LoadBalancer", "own.tls.secretName=relay-own-host-tls")
    assert not [d for d in documents if d["kind"] == "Ingress"]
    own = _one(documents, "Service", "-own")
    assert own["spec"]["type"] == "LoadBalancer" and own["spec"]["externalTrafficPolicy"] == "Local"
    container = _relay(documents)
    env = _literal_env(container)
    volumes = {v["name"]: v for v in _pod(documents)["volumes"]}
    assert volumes["own-tls"]["secret"]["secretName"] == "relay-own-host-tls"
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert env["TRANIBOR_RELAY_OWN_TLS_CERT_FILE"] == f"{mounts['own-tls']}/tls.crt"
    assert env["TRANIBOR_RELAY_OWN_TLS_KEY_FILE"] == f"{mounts['own-tls']}/tls.key"
    assert env["TRANIBOR_RELAY_OWN_PUBLIC_PORT"] == str(own["spec"]["ports"][0]["port"])
    # Exposed without TLS, or with two places that would terminate it: refused.
    assert "own.tls.secretName" in _refusal(*MINIMAL, "own.service.type=LoadBalancer")
    assert "own.tls.secretName" in _refusal(
        *MINIMAL, "own.ingress.enabled=true", "own.tls.secretName=relay-own-host-tls"
    )


# ───────────────────────── the pod ─────────────────────────


def test_the_relay_runs_as_non_root_with_a_read_only_root_filesystem() -> None:
    documents = _documents(values=EXAMPLE)
    pod = _pod(documents)
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0 and pod["securityContext"]["runAsGroup"] != 0
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["automountServiceAccountToken"] is False
    # Service links would hand the relay `<SERVICE>_PORT=tcp://…` variables under its own prefix.
    assert pod["enableServiceLinks"] is False
    assert not pod.get("initContainers")
    container = _relay(documents)
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert set(container["resources"]) == {"requests", "limits"}
    assert set(container["resources"]["limits"]) >= {"cpu", "memory"}
    # Writable: the metadata lines' directory and a scratch directory, nothing else.
    writable = sorted(m["mountPath"] for m in container["volumeMounts"] if not m.get("readOnly"))
    assert writable == sorted([METADATA_DIR, TMP])
    from tranibor_sni_relay.server import relay_config

    config = relay_config(_literal_env(container))
    assert config.metadata_path.parent == pathlib.Path(METADATA_DIR)
    # No port below 1024 inside the pod: no capability to bind one.
    assert all(int(p["containerPort"]) >= 1024 for p in container["ports"])
    # Probed on the own host only: a probe of the SNI listener would be a metadata line every time.
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["tcpSocket"] == {"port": "own"}, probe


def test_the_service_account_is_bound_to_no_role_and_its_token_is_not_mounted() -> None:
    documents = _documents(*MINIMAL)
    account = _one(documents, "ServiceAccount")
    assert account["automountServiceAccountToken"] is False
    assert _pod(documents)["serviceAccountName"] == account["metadata"]["name"]
    bindings = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
    assert not [d for d in documents if d["kind"] in bindings]


def test_one_relay_process_and_a_budget_that_stops_a_drain_at_it() -> None:
    """The relay keeps its control sessions and its registry in one process: a second replica would
    close every phone whose installation dialled the other; a restart forgets every pairing."""
    documents = _documents(*MINIMAL)
    deployment = _one(documents, "Deployment")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    budget = _one(documents, "PodDisruptionBudget")
    assert budget["spec"]["minAvailable"] == 1
    assert budget["spec"]["selector"]["matchLabels"].items() <= (
        deployment["spec"]["template"]["metadata"]["labels"].items()
    )
    relaxed = _one(_documents(*MINIMAL, "podDisruptionBudget.maxUnavailable=1"), "PodDisruptionBudget")
    assert relaxed["spec"] == {"maxUnavailable": 1, "selector": budget["spec"]["selector"]}
    # The metadata lines outlive the pod, on a claim of their own.
    claim = _one(documents, "PersistentVolumeClaim")
    volumes = {v["name"]: v for v in _pod(documents)["volumes"]}
    assert volumes["metadata"]["persistentVolumeClaim"]["claimName"] == claim["metadata"]["name"]


def test_the_network_policy_admits_the_two_listeners_and_lets_out_dns_alone() -> None:
    documents = _documents(*MINIMAL)
    policy = _one(documents, "NetworkPolicy")
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    admitted = sorted(port["port"] for rule in policy["spec"]["ingress"] for port in rule["ports"])
    assert admitted == ["own", "sni"]
    (dns,) = policy["spec"]["egress"]
    assert sorted((p["protocol"], p["port"]) for p in dns["ports"]) == [("TCP", 53), ("UDP", 53)]


# ───────────────────────── what the relay reads ─────────────────────────


def test_the_relay_starts_on_what_the_chart_writes() -> None:
    from tranibor_sni_relay.server import relay_config

    container = _relay(_documents("relay.limits.concurrentStreams=7", values=EXAMPLE))
    config = relay_config(_literal_env(container))
    assert config.domain == DOMAIN
    assert config.retention == timedelta(days=30)
    assert config.limits.concurrent_streams == 7
    assert config.idle_timeout_s == 900.0


@pytest.mark.parametrize(
    ("sets", "names"),
    [
        (("relay.domain=",), "relay.domain"),
        (("relay.domain=Relay.Example.com",), "relay.domain"),
        (("relay.retentionDays=0",), "relay.retentionDays"),
        (("relay.hostname=abcdefghijkl.relay.example.com",), "relay.hostname"),
        (("relay.limits.streamsPerMinute=0",), "relay.limits.streamsPerMinute"),
    ],
)
def test_a_configuration_the_relay_would_refuse_fails_the_render(sets: tuple[str, ...], names: str) -> None:
    assert names in _refusal(*MINIMAL, *sets)


# ───────────────────────── linted beside the others ─────────────────────────


def test_the_chart_is_linted_and_validated_beside_the_platforms() -> None:
    lint = "helm lint charts/choregos-relay -f charts/choregos-relay/values/example.yaml"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("charts-lint:", 1)[1].split(".PHONY", 1)[0]
    assert lint in target
    assert "helm template relay charts/choregos-relay -f charts/choregos-relay/values/example.yaml" in target
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = "\n".join(str(step.get("run", "")) for step in ci["jobs"]["charts"]["steps"])
    assert lint in steps and "/tmp/relay.yaml" in steps
    kinds = sorted(d["kind"] for d in _documents(values=EXAMPLE))
    assert kinds == [
        "Deployment",
        "Ingress",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "PodDisruptionBudget",
        "Service",
        "Service",
        "ServiceAccount",
    ]
