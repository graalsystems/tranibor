"""The chart takes the name tranibor (ADR 0226, R6): its keys, its objects, what keeps its name.

* A component is set under `tranibor-<component>`; its former key `choregos-<component>` is read too for
  the window, through one helper (`templates/_values.tpl`), and renders the same objects; a setting
  written under both names refuses to render and names both.
* The objects the chart renders are `tranibor-*` (or named after the release), except the short list
  of names something outside the chart holds: application code (R7), an in-flight run, or data.
* What holds data keeps its name: the claims and the StatefulSets that own them, and their selectors,
  which Kubernetes does not let an upgrade change.
* The queue label is rendered under both names; the policy that lets the executor in admits either.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"
OUTPOST = ROOT / "charts" / "tranibor-outpost"
OPERATOR = ["--api-versions", "extensions.agents.x-k8s.io/v1beta1/SandboxTemplate"]
SITE = ["--set", "global.bootstrap.org=acme", "--set", "global.bootstrap.admins=root@acme.example"]
ALL_IN_ONE = ["-f", str(CHART / "values" / "all-in-one.yaml"), *SITE]
#: A build registry: the chart then runs its own builder (`tranibor-buildkitd`).
BUILDS = "global.images.buildRegistry=registry.internal/builds"
OUTPOSTS = [
    "--set",
    "global.outposts.enabled=true",
    "--set",
    "global.outposts.door.hostnames={door.example.com}",
]

#: The names the chart keeps under the former name, each with the reason (docs/deployment.md, "Upgrading
#: to the next release: the chart takes the name tranibor").
KEPT = {
    ("Service", "choregos-api"): "the API's former address, for runs started before the upgrade",
    ("ServiceAccount", "choregos-orchestrator"): "gitops.py binds it by name in every project namespace",
    ("ServiceAccount", "choregos-runner"): "the k8s_job executor's default service account (R7)",
    ("SandboxTemplate", "choregos-runner"): "its claim template holds the suspended sandboxes' workspaces",
    ("SandboxWarmPool", "choregos-runner"): "the claims name it, and it names the template",
    ("Deployment", "choregos-egress"): "gitops.py's per-project proxy has the same name (R7)",
    ("Service", "choregos-egress"): "one default `runner.egressProxy` addresses both proxies",
}
#: A Secret's default name is stored state (ADR 0226 §6): the Secrets the chart draws, and the
#: ExternalSecret whose target is one, keep theirs (`test_secret_names_are_stored_state.py`).
SECRET_KINDS = {"Secret", "ExternalSecret"}

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(
    *arguments: str, chart: pathlib.Path = CHART, release: str = "platform"
) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", release, str(chart), "-n", "platform", *OPERATOR, *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)


def _docs(*arguments: str, chart: pathlib.Path = CHART, release: str = "platform") -> list[dict[str, Any]]:
    rendered = _render(*arguments, chart=chart, release=release)
    assert rendered.returncode == 0, rendered.stderr
    docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    for doc in docs:
        # Drawn at random on every render (Temporal's codec key, the bench's secrets): not a difference.
        if doc["kind"] == "Secret":
            doc.pop("stringData", None)
            doc.pop("data", None)
    return docs


def _comparable(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The documents, without the checksums of drawn values (a pod's `checksum/*` annotations)."""
    text = json.dumps(docs, sort_keys=True)
    out: list[dict[str, Any]] = json.loads(text)
    for doc in out:
        template = ((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}
        for key in list(template.get("annotations") or {}):
            if key.startswith("checksum/"):
                del template["annotations"][key]
    return out


# ───────────────────────────── the values keys ─────────────────────────────

#: Settings of several components, written as a values file: false, 0, [] and "" included, each of
#: which replaces the default as it does under the new key.
SETTINGS: dict[str, Any] = {
    "api": {
        "replicaCount": 3,
        "autoscaling": {"enabled": False},
        "runDoor": {"hostnames": ["run.example.com"]},
    },
    "web": {"podDisruptionBudget": {"enabled": False}, "ingress": {"hostnames": [], "gateway": ""}},
    "orchestrator": {"unreachableTolerationSeconds": 0, "runner": {"egressGate": {"enabled": False}}},
    "websearch": {"enabled": True},
}


def _values_file(tmp_path: pathlib.Path, prefix: str, settings: dict[str, Any]) -> list[str]:
    path = tmp_path / f"{prefix}.yaml"
    path.write_text(yaml.safe_dump({f"{prefix}-{k}": v for k, v in settings.items()}), encoding="utf-8")
    return ["-f", str(path)]


@helm
def test_the_former_keys_render_what_the_new_keys_render(tmp_path: pathlib.Path) -> None:
    new = _comparable(_docs(*_values_file(tmp_path, "tranibor", SETTINGS)))
    former = _comparable(_docs(*_values_file(tmp_path, "choregos", SETTINGS)))
    default = _comparable(_docs())
    assert new == former
    assert new != default, "the settings changed nothing: the test proves nothing"
    api = next(d for d in new if d["kind"] == "Deployment" and d["metadata"]["name"] == "tranibor-api")
    assert api["spec"]["replicas"] == 3, "false and 3 were both read (`autoscaling.enabled: false`)"
    assert not [d for d in new if d["kind"] == "HorizontalPodAutoscaler"]
    assert [d for d in new if d["metadata"]["name"].endswith("-searxng")], "the subchart's condition read it"
    (route,) = [d for d in new if d["kind"] == "HTTPRoute" and d["metadata"]["name"] == "tranibor-web"]
    assert route["spec"]["hostnames"] is None, "an empty list replaces the default, it is not merged away"
    assert route["spec"]["parentRefs"][0]["name"] is None, "so does an empty string"


@helm
def test_each_name_may_hold_part_of_the_settings(tmp_path: pathlib.Path) -> None:
    """A values file half moved: what is left under the former key and what moved are both read."""
    split = _values_file(tmp_path, "tranibor", {"api": SETTINGS["api"], "web": SETTINGS["web"]})
    split += _values_file(
        tmp_path, "choregos", {"orchestrator": SETTINGS["orchestrator"], "websearch": {"enabled": True}}
    )
    rendered = _comparable(_docs(*split))
    assert rendered == _comparable(_docs(*_values_file(tmp_path, "tranibor", SETTINGS)))
    assert rendered != _comparable(_docs()), "neither name was read"


@helm
@pytest.mark.parametrize(
    ("current", "former", "conflict"),
    [
        ("tranibor-api.replicaCount=3", "choregos-api.replicaCount=3", True),
        ("tranibor-api.image.tag=1", "choregos-api.image.repository=x", False),
        ("tranibor-web.ingress.hostnames={a.example}", "choregos-web.ingress={}", True),
        ("tranibor-websearch.enabled=true", "choregos-websearch.enabled=false", True),
    ],
)
def test_a_setting_under_both_names_refuses_to_render(current: str, former: str, conflict: bool) -> None:
    rendered = _render("--set", current, "--set", former)
    if not conflict:
        assert rendered.returncode == 0, "two different settings of one component are no conflict"
        return
    assert rendered.returncode != 0, "the chart chose one of two values"
    named = f"{current.split('=', 1)[0]} and {former.split('=', 1)[0]} are both set"
    assert named in rendered.stderr and "ADR 0226" in rendered.stderr


@helm
def test_a_component_s_values_are_a_map() -> None:
    rendered = _render("--set", "choregos-api=on")
    assert rendered.returncode != 0 and "choregos-api: a component's values are a map" in rendered.stderr


def test_every_subchart_is_read_under_both_names() -> None:
    """The helper's list is the Chart.yaml's aliases: a subchart added without it would read neither key."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
    aliases = [dependency["alias"] for dependency in chart["dependencies"]]
    assert [dependency["name"] for dependency in chart["dependencies"]] == [f"tranibor-{a}" for a in aliases]
    helpers = (CHART / "templates" / "_values.tpl").read_text(encoding="utf-8")
    listed = helpers.split('{{- define "tranibor.components" -}}\n', 1)[1].split("\n", 1)[0].split()
    assert sorted(listed) == sorted(aliases)
    conditions = {d["alias"]: d["condition"].split(",") for d in chart["dependencies"] if "condition" in d}
    for alias, paths in conditions.items():
        if not paths[0].startswith("global."):
            assert paths == [f"tranibor-{alias}.enabled", f"choregos-{alias}.enabled", f"{alias}.enabled"]
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert not [key for key in values if key.startswith(("tranibor-", "choregos-"))], (
        "a default under an operator's key would be read as the operator's own setting"
    )


def test_the_merge_renders_before_every_other_template() -> None:
    """Helm renders the deepest path first: the merge must stay deeper than any template that reads values."""
    first = CHART / "templates" / "values" / "keys" / "merged" / "first.yaml"
    depth = len(first.relative_to(CHART).parts)
    others = [
        p
        for p in CHART.rglob("*")
        if p.is_file()
        and "templates" in p.relative_to(CHART).parts
        and not p.name.startswith("_")
        and p != first
    ]
    assert others
    deeper = [str(p.relative_to(CHART)) for p in others if len(p.relative_to(CHART).parts) >= depth]
    assert not deeper, f"rendered no later than the merge: {deeper}"


# ───────────────────────────── the objects ─────────────────────────────


@helm
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        [*ALL_IN_ONE, "--set", "tranibor-websearch.enabled=true", "--set", "backup.enabled=true"],
        ["--set", "global.egress.enabled=true", "--set", "demoFakes.enabled=true", "--set", BUILDS],
        [*OUTPOSTS, "--set", "global.outposts.door.dedicated=true", "--set", "monitoring.alerts=true"],
    ],
    ids=["default", "all-in-one", "egress-fakes-builder", "outposts-monitoring"],
)
def test_the_objects_are_named_tranibor_or_after_the_release(arguments: list[str]) -> None:
    named = {(d["kind"], d["metadata"]["name"]) for d in _docs(*arguments)}
    stray = sorted(
        (kind, name)
        for kind, name in named
        if not name.startswith(("tranibor", "platform-"))
        and (kind, name) not in KEPT
        and kind not in SECRET_KINDS
    )
    assert not stray, f"neither tranibor-* nor the release's: {stray}"
    assert ("Deployment", "tranibor-api") in named or not arguments


@helm
def test_the_api_keeps_its_former_address_for_the_runs_in_flight() -> None:
    docs = _docs("--set", "monitoring.serviceMonitor=true")
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    current, former = services["tranibor-api"], services["choregos-api"]
    assert former["spec"]["selector"] == current["spec"]["selector"]
    assert former["spec"]["ports"] == current["spec"]["ports"]
    (monitor,) = [d for d in docs if d["kind"] == "ServiceMonitor"]
    selected = monitor["spec"]["selector"]["matchLabels"].items()
    assert selected <= current["metadata"]["labels"].items()
    assert not selected <= former["metadata"]["labels"].items(), "the API's pods would be scraped twice"
    for deployment in (d for d in docs if d["kind"] == "Deployment"):
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            urls = [e.get("value", "") for e in container.get("env", []) if e["name"].endswith("_API_URL")]
            assert not [u for u in urls if "choregos-api" in u], f"{deployment['metadata']['name']}: {urls}"


# ───────────────────────────── what holds data ─────────────────────────────


@helm
def test_what_holds_data_keeps_its_name_and_its_selector() -> None:
    """Claims and the StatefulSets that own them are named after the release, never after the chart,
    and their selectors are what the former chart rendered: a rename would orphan the data, and a
    StatefulSet's selector cannot change on upgrade."""
    docs = _docs(*ALL_IN_ONE, "--set", "backup.enabled=true", "--set", "global.temporal.embedded=true")
    claims = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"}
    assert claims == {"platform-backup", "platform-ecphoria", "platform-objectstore", "platform-temporal"}
    (postgresql,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert postgresql["metadata"]["name"] == "platform-postgresql"
    assert postgresql["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "choregos-postgresql",
        "app.kubernetes.io/instance": "platform",
    }
    (template,) = [d for d in docs if d["kind"] == "SandboxTemplate"] or [None]
    if template is not None:
        assert template["metadata"]["name"] == "choregos-runner"


@helm
@pytest.mark.parametrize(
    ("release", "statefulset", "objects"),
    [
        ("factory", "factory-choregos-outpost", "factory-tranibor-outpost"),
        ("choregos-outpost", "choregos-outpost", "choregos-outpost"),
        ("tranibor-outpost", "tranibor-outpost-choregos-outpost", "tranibor-outpost"),
    ],
)  # fmt: skip
def test_an_outpost_keeps_its_statefulset_and_its_claims(
    release: str, statefulset: str, objects: str
) -> None:
    example = ["-f", str(OUTPOST / "values" / "example.yaml"), "--set", "fullnameOverride="]
    docs = _docs(*example, chart=OUTPOST, release=release)
    (daemon,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert daemon["metadata"]["name"] == statefulset and daemon["spec"]["serviceName"] == statefulset
    # Its claims are `data-<statefulset>-N`; their template and the selector are the former chart's.
    assert [c["metadata"]["name"] for c in daemon["spec"]["volumeClaimTemplates"]] == ["data"]
    assert daemon["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"] == "choregos-outpost"
    pod = daemon["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == objects
    assert {"ServiceAccount", "ConfigMap", "NetworkPolicy"} <= {
        d["kind"] for d in docs if d["metadata"]["name"] in {objects, f"{objects}-policy"}
    }
    moved = _docs(*example, "--set", "persistence.statefulSetName=moved", chart=OUTPOST, release=release)
    (daemon,) = [d for d in moved if d["kind"] == "StatefulSet"]
    assert daemon["metadata"]["name"] == "moved" == daemon["spec"]["serviceName"]


# ───────────────────────────── the labels ─────────────────────────────


@helm
def test_the_queue_label_is_rendered_under_both_names() -> None:
    docs = _docs()
    workers = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].startswith("tranibor-orchestrator-")
    ]
    assert len(workers) == 4
    for worker in workers:
        queue = worker["metadata"]["name"].removeprefix("tranibor-orchestrator-")
        for labels in (worker["metadata"]["labels"], worker["spec"]["template"]["metadata"]["labels"]):
            assert labels["tranibor/queue"] == labels["choregos/queue"] == queue
        assert worker["spec"]["selector"]["matchLabels"]["tranibor/queue"] == queue
    budgets = [
        d
        for d in docs
        if d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"].startswith("tranibor-orchestrator-")
    ]
    assert budgets and all(b["metadata"]["labels"]["choregos/queue"] for b in budgets)
    (policy,) = [d for d in docs if d["metadata"]["name"] == "tranibor-runner-assignment"]
    admitted = [peer["podSelector"]["matchLabels"] for peer in policy["spec"]["ingress"][0]["from"]]
    assert {"tranibor/queue": "executor"}.items() <= admitted[0].items()
    assert {"choregos/queue": "executor"}.items() <= admitted[1].items()
