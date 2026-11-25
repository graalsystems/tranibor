"""What the chart assumed about a cluster is a setting, or follows the cluster (S87-16).

The audit of 2026-11-25 (`docs/plan/PLAN-2026-11-25-isolation-everywhere-and-clouds.md`, "The
clusters") found that a managed cloud would refuse the chart's defaults before the first run: no
`kubeVersion` though the default isolation needs native sidecars, DNS opened to `kube-system:53`
only, the Prometheus Operator's types rendered with no check, an Ingress with no TLS, the door's
timeouts written for ingress-nginx alone, External Secrets at `v1beta1` only, and nothing said about
what a managed cluster must open. One test per item, each red with its change removed; and, beside
them, the defaults render what they always rendered, so the operator's dev and prod do not move.

Nothing here was run on EKS, GKE, AKS or OpenShift: the provider-shaped values come from their
documentation (docs/deployment.md, "On a managed cluster").
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

from .conftest import MONITORING_APIS, OPERATOR_API

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "choregos"
VALUES = CHART / "values.yaml"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"

#: Every policy of the chart that opens DNS: the outposts' door, the builder and the egress proxy
#: render only when switched on.
EVERYTHING = (
    "global.outposts.enabled=true",
    'global.outposts.door.hostnames=["outposts.example.com"]',
    "global.outposts.door.dedicated=true",
    "global.images.buildRegistry=registry.internal/builds",
    "global.egress.enabled=true",
)
DNS_POLICIES = {
    "choregos-allow-platform",
    "choregos-runner-egress",
    "choregos-egress-net",
    "choregos-outposts-door-net",
    "choregos-buildkitd-net",
}
OPENSHIFT_DNS = [{"port": 5353, "protocol": "UDP"}, {"port": 5353, "protocol": "TCP"}]

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*sets: str, apis: tuple[str, ...] | None = None, kube_version: str | None = None) -> Any:
    """`helm template` with `--set-json` for each `key=value` (JSON values, or a bare string). `apis`:
    the cluster's API versions, exactly; None: the conftest's (the operator's cluster)."""
    command = ["helm", "template", "choregos", str(CHART), "-n", "choregos"]
    for item in sets:
        key, _, value = item.partition("=")
        try:
            json.loads(value)
        except ValueError:
            value = json.dumps(value)
        command += ["--set-json", f"{key}={value}"]
    for api in apis or ():
        command += ["--api-versions", api]
    if kube_version:
        command += ["--kube-version", kube_version]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)


def _documents(*sets: str, apis: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    rendered = _render(*sets, apis=apis)
    assert rendered.returncode == 0, rendered.stderr
    return [d for d in yaml.safe_load_all(rendered.stdout) if d]


def _named(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} rendered"
    return found[0]


def _dns_rules(documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each policy's DNS rule: the egress rule whose ports include 53 or 5353."""
    rules: dict[str, list[dict[str, Any]]] = {}
    for policy in (d for d in documents if d["kind"] == "NetworkPolicy"):
        for rule in policy["spec"].get("egress") or []:
            if any(p.get("port") in (53, 5353) for p in rule.get("ports") or []):
                rules.setdefault(policy["metadata"]["name"], []).append(rule)
    return rules


# 1. kubeVersion ------------------------------------------------------------------------------------


def test_the_chart_refuses_a_cluster_without_native_sidecars() -> None:
    """The default isolation runs the agent as a native sidecar (Kubernetes 1.29+): Helm and Argo CD
    must refuse an older cluster at render, not let the first run fail."""
    refused = _render(kube_version="1.28.9")
    assert refused.returncode != 0
    assert "kubeVersion" in refused.stderr and ">=1.29.0-0" in refused.stderr
    # The providers' versions carry a suffix, which a bare `>=1.29.0` would read as a pre-release.
    for version in ("1.29.0", "v1.29.4-eks-0a21954", "v1.30.5-gke.1014001", "v1.31.2"):
        rendered = _render(kube_version=version)
        assert rendered.returncode == 0, (version, rendered.stderr)
    assert yaml.safe_load((CHART / "Chart.yaml").read_text())["kubeVersion"] == ">=1.29.0-0"


# 2. DNS --------------------------------------------------------------------------------------------


def test_the_default_dns_rules_are_the_ones_every_policy_always_rendered() -> None:
    """Byte for byte: the operator's dev and prod policies do not move. The platform's own pods open
    UDP 53; the runner pods, the egress proxy, the door and the builder UDP and TCP 53."""
    rendered = _render(*EVERYTHING)
    assert rendered.returncode == 0, rendered.stderr
    to = "    - to: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } } }]\n"
    assert rendered.stdout.count(to + "      ports: [{ protocol: UDP, port: 53 }]\n") == 1
    both = to + "      ports: [{ protocol: UDP, port: 53 }, { protocol: TCP, port: 53 }]\n"
    assert rendered.stdout.count(both) == 4
    assert set(_dns_rules([d for d in yaml.safe_load_all(rendered.stdout) if d])) == DNS_POLICIES


def test_openshift_dns_is_opened_in_its_namespace_on_its_port() -> None:
    """OpenShift's resolver is the pods of `openshift-dns`, listening on 5353: every policy opens
    that, and none keeps `kube-system`."""
    documents = _documents(
        *EVERYTHING,
        "networkPolicies.dns.namespace=openshift-dns",
        f"networkPolicies.dns.ports={json.dumps(OPENSHIFT_DNS)}",
    )
    rules = _dns_rules(documents)
    assert set(rules) == DNS_POLICIES
    for name, found in rules.items():
        assert found == [
            {
                "to": [
                    {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}}
                ],
                "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}],
            }
        ], name
    left = [
        d["metadata"]["name"]
        for d in documents
        if d["kind"] == "NetworkPolicy" and "kube-system" in json.dumps(d)
    ]
    assert not left, f"policies still opening kube-system: {left}"


def test_a_node_local_cache_and_the_resolver_pods_can_be_named() -> None:
    """GKE's NodeLocal DNSCache answers at a link-local address no namespace holds: an `ipBlock`
    beside the namespace. The resolver's own pods can be selected within the namespace."""
    documents = _documents(
        *EVERYTHING,
        'networkPolicies.dns.cidrs=["169.254.20.10/32"]',
        'networkPolicies.dns.podLabels={"k8s-app":"kube-dns"}',
    )
    for name, (rule,) in _dns_rules(documents).items():
        assert rule["to"] == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            },
            {"ipBlock": {"cidr": "169.254.20.10/32"}},
        ], name
    # The cache alone, no namespace.
    alone = _dns_rules(
        _documents('networkPolicies.dns.namespace=""', 'networkPolicies.dns.cidrs=["169.254.20.10/32"]')
    )
    assert alone and all(
        rules[0]["to"] == [{"ipBlock": {"cidr": "169.254.20.10/32"}}] for rules in alone.values()
    )


@pytest.mark.parametrize(
    ("sets", "said"),
    [
        (('networkPolicies.dns.namespace=""',), "neither a namespace nor cidrs"),
        (('networkPolicies.dns.ports=[{"port":53}]',), "networkPolicies.dns.ports"),
        (
            (
                'networkPolicies.dns.namespace=""',
                'networkPolicies.dns.cidrs=["10.0.0.10/32"]',
                'networkPolicies.dns.podLabels={"a":"b"}',
            ),
            "podLabels",
        ),
    ],
)
def test_a_dns_setting_that_opens_nothing_is_refused(sets: tuple[str, ...], said: str) -> None:
    refused = _render(*sets)
    assert refused.returncode != 0 and said in refused.stderr


# 3. Monitoring --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flavour", "scrape", "rules"),
    [("prometheus", "ServiceMonitor", "PrometheusRule"), ("victoriametrics", "VMServiceScrape", "VMRule")],
)
def test_the_monitoring_objects_follow_what_the_cluster_serves(flavour: str, scrape: str, rules: str) -> None:
    """Without the operator's CRDs a ServiceMonitor fails the whole install (`no matches for kind`):
    the chart renders each object only when the cluster serves its type."""
    flavoured = f"monitoring.flavour={flavour}"

    def kinds(*sets: str, apis: tuple[str, ...]) -> set[str]:
        return {d["kind"] for d in _documents(flavoured, *sets, apis=apis)} & {scrape, rules}

    assert kinds(apis=(OPERATOR_API,)) == set(), "rendered on a cluster without the operator"
    assert kinds(apis=(OPERATOR_API, *MONITORING_APIS)) == {scrape, rules}
    served = tuple(api for api in MONITORING_APIS if api.endswith("/" + scrape))
    assert kinds(apis=(OPERATOR_API, *served)) == {scrape}
    # The other flavour's operator does not count: its types are not these.
    other = tuple(api for api in MONITORING_APIS if not api.endswith(("/" + scrape, "/" + rules)))
    assert kinds(apis=(OPERATOR_API, *other)) == set()
    # An offline render aimed at a cluster that has the operator says so.
    assert kinds("monitoring.skipCrdCheck=true", apis=(OPERATOR_API,)) == {scrape, rules}
    # And the switches still switch them off.
    assert (
        kinds(
            "monitoring.serviceMonitor=false",
            "monitoring.alerts=false",
            apis=(OPERATOR_API, *MONITORING_APIS),
        )
        == set()
    )


@pytest.fixture
def reachable_cluster() -> None:
    """`helm install --dry-run=client` reads the API server's version (the chart's `kubeVersion`) and its
    discovery (to map each object to a kind): it needs a cluster, which CI does not have. Without one the
    test says so and skips; `test_the_monitoring_objects_follow_what_the_cluster_serves` holds the
    same rule offline, on the render."""
    probe = subprocess.run(
        ["kubectl", "version", "-o", "json"], capture_output=True, text=True, check=False, timeout=30
    )
    if probe.returncode != 0 or '"serverVersion"' not in probe.stdout:
        pytest.skip(
            "no reachable cluster in the current kube context: helm's client dry-run needs its discovery"
        )


@pytest.mark.usefixtures("reachable_cluster")
def test_an_install_on_a_cluster_without_the_operator_goes_through_and_says_so() -> None:
    """`helm install --dry-run=client` maps every object against the kinds a bare cluster serves,
    as the API server would: the default monitoring no longer stops the install, and the notes say
    that nothing will be scraped. (The other CRD-backed defaults are switched to built-in kinds.)"""
    command = [
        *("helm", "install", "choregos", str(CHART), "-n", "choregos", "--dry-run=client"),
        *("--set", "global.externalSecrets.enabled=false", "--set", "global.runner.executor=k8s_job"),
        *("--set", "choregos-api.ingress.kind=ingress", "--set", "choregos-web.ingress.kind=ingress"),
    ]
    installed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
    assert installed.returncode == 0, installed.stderr
    notes = installed.stdout[installed.stdout.index("NOTES:") :]
    assert "does not serve monitoring.coreos.com/v1/ServiceMonitor" in notes
    assert "monitoring.skipCrdCheck" in notes


# 4. Ingress TLS ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("component", ["choregos-api", "choregos-web"])
def test_the_ingress_kind_carries_a_tls_block_when_given_one(component: str) -> None:
    tls = [{"secretName": "choregos-tls", "hosts": ["api.example.com", "app.example.com"]}]
    kind = f"{component}.ingress.kind=ingress"
    ingress = _named(_documents(kind, f"{component}.ingress.tls={json.dumps(tls)}"), "Ingress", component)
    assert ingress["spec"]["tls"] == tls
    assert "tls" not in _named(_documents(kind), "Ingress", component)["spec"]


# 5. The outposts door's timeouts ----------------------------------------------------------------------

DOOR_ON = (
    "global.outposts.enabled=true",
    'global.outposts.door.hostnames=["outposts.example.com"]',
    "choregos-api.ingress.kind=ingress",
)
NGINX = {
    "nginx.ingress.kubernetes.io/proxy-read-timeout": "60",
    "nginx.ingress.kubernetes.io/proxy-buffering": "off",
}


def _door_annotations(*sets: str) -> dict[str, str]:
    door = _named(_documents(*DOOR_ON, *sets), "Ingress", "choregos-outposts-door")
    return dict(door["metadata"].get("annotations") or {})


def test_the_door_timeouts_are_written_for_the_controller_named() -> None:
    """ingress-nginx's by default, as before; another controller's verbatim, without ingress-nginx's
    beside them: Helm merges a values map into the chart's, so a map default alone could not be
    replaced."""
    assert _door_annotations() == NGINX
    # An installation that added a rate limit keeps its timeouts.
    assert _door_annotations(
        'global.outposts.door.annotations={"nginx.ingress.kubernetes.io/limit-rps":"5"}'
    ) == {
        **NGINX,
        "nginx.ingress.kubernetes.io/limit-rps": "5",
    }
    openshift = {"haproxy.router.openshift.io/timeout": "60s"}
    assert (
        _door_annotations(
            'global.outposts.door.ingressController=""',
            f"global.outposts.door.annotations={json.dumps(openshift)}",
        )
        == openshift
    )
    assert _door_annotations('global.outposts.door.ingressController=""') == {}
    refused = _render(*DOOR_ON, "global.outposts.door.ingressController=traefik")
    assert refused.returncode != 0 and "global.outposts.door.ingressController" in refused.stderr


# 6. External Secrets ------------------------------------------------------------------------------------

ESO_V1 = "external-secrets.io/v1/ExternalSecret"


def _external_secret_version(*sets: str, apis: tuple[str, ...]) -> str:
    documents = _documents("global.externalSecrets.enabled=true", *sets, apis=apis)
    return str(_named(documents, "ExternalSecret", "choregos-api-secrets")["apiVersion"])


def test_the_external_secret_follows_the_version_the_cluster_serves() -> None:
    assert _external_secret_version(apis=(OPERATOR_API,)) == "external-secrets.io/v1beta1"
    assert _external_secret_version(apis=(OPERATOR_API, ESO_V1)) == "external-secrets.io/v1"
    forced = "global.externalSecrets.apiVersion=external-secrets.io/v1"
    assert _external_secret_version(forced, apis=(OPERATOR_API,)) == "external-secrets.io/v1"
    older = "global.externalSecrets.apiVersion=external-secrets.io/v1beta1"
    assert _external_secret_version(older, apis=(OPERATOR_API, ESO_V1)) == "external-secrets.io/v1beta1"
    refused = _render("global.externalSecrets.enabled=true", "global.externalSecrets.apiVersion=v2")
    assert refused.returncode != 0 and "global.externalSecrets.apiVersion" in refused.stderr


# 7. What a managed cluster must open --------------------------------------------------------------------


def _platform_egress_examples() -> list[dict[str, Any]]:
    """The commented examples under `networkPolicies.platformEgress` in values.yaml, uncommented."""
    text = VALUES.read_text()
    block = text[text.index("  # Egress entries (NetworkPolicy `egress` rules") :]
    block = block[: block.index("  platformEgress: []")]
    lines = [m.group(1) for m in re.finditer(r"^  #   (.*)$", block, re.MULTILINE)]
    examples = yaml.safe_load("\n".join(line for line in lines if not line.lstrip().startswith("#")))
    assert isinstance(examples, list) and examples, "no example under networkPolicies.platformEgress"
    return examples


def test_the_platform_egress_examples_render_into_the_platform_policy() -> None:
    """The examples a platform team adapts are rules the chart accepts as written: the API server on
    443 and a managed PostgreSQL on 5432, in `choregos-allow-platform`."""
    examples = _platform_egress_examples()
    ports = {p["port"] for rule in examples for p in rule["ports"]}
    assert {443, 5432} <= ports
    policy = _named(
        _documents(f"networkPolicies.platformEgress={json.dumps(examples)}"),
        "NetworkPolicy",
        "choregos-allow-platform",
    )
    for rule in examples:
        assert rule in policy["spec"]["egress"]


def test_the_deployment_guide_says_what_a_managed_cluster_needs() -> None:
    """Every new value is documented, with what to open per provider, and the guide says none of it
    was run there."""
    text = DEPLOYMENT_DOC.read_text()
    section = text[text.index("## On a managed cluster") :]
    section = section[: section.index("\n## ", 1)]
    for needed in (
        "kubeVersion",
        "networkPolicies.dns",
        "openshift-dns",
        "5353",
        "169.254.20.10/32",
        "monitoring.skipCrdCheck",
        "ingress.tls",
        "door.ingressController",
        "global.externalSecrets.apiVersion",
        "platformEgress",
        "EKS",
        "GKE",
        "AKS",
        "OpenShift",
        "not been run",
    ):
        assert needed in section, needed
