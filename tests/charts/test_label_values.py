"""ADR 0226 (R9b): the values of `app.kubernetes.io/name` and `part-of` on the platform's own pods.

A label has one value, so it cannot carry a twin. An operator's NetworkPolicies, Cilium policies, log and
metric queries may select the former value (`choregos-api`, `part-of: choregos`): renamed in place, they
would stop matching without a word. So the former values stay the default for the window, and
`global.labelValues: tranibor` moves a deployment once its own selectors admit either value. What must hold
for the switch to be safe:

- the chart's own policies, and the code's selectors, admit either value (the NetworkPolicy tests);
- no Deployment, StatefulSet, Service or budget selects on those values, so the switch rolls pods and
  recreates nothing, and a StatefulSet's (immutable) selector is the same both ways;
- every selector still selects its own pods both ways;
- a value the chart does not know is refused.
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

from .conftest import NAME_LABEL

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
PART_OF = "app.kubernetes.io/part-of"
SITE = ("global.bootstrap.org=acme", "global.bootstrap.admins=root@acme.example")
#: The renders that reach every pod of the chart between them.
RENDERS = {
    "default": (),
    "all-in-one": ("-f", str(CHART / "values" / "all-in-one.yaml"), *(a for s in SITE for a in ("--set", s))),
    "everything": (
        "-f",
        str(CHART / "values" / "local.yaml"),
        *(
            a
            for s in (
                "demoFakes.enabled=true",
                "demoFakes.tokenSecret.name=demo",
                "demoFakes.tokenSecret.key=token",
                "global.images.buildRegistry=registry.internal/builds",
                "global.outposts.enabled=true",
                "global.outposts.door.dedicated=true",
                "global.outposts.door.url=https://door.example.com",
                "global.tracing.tempoUrl=http://tempo:3200",
                "backup.enabled=true",
                "backup.target.s3.url=s3://backups/x",
            )
            for a in ("--set", s)
        ),
    ),
}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "platform", str(CHART), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)


def _comparable(text: str) -> list[dict[str, Any]]:
    """The documents, without what is drawn at random on every render (secrets and their checksums)."""
    docs = [d for d in yaml.safe_load_all(text) if d]
    for doc in docs:
        if doc["kind"] == "Secret":
            doc.pop("stringData", None)
            doc.pop("data", None)
    return json.loads(re.sub(r'"checksum/[^"]+": "[0-9a-f]+"', '"checksum": ""', json.dumps(docs)))


def _docs(render: str, values: str) -> list[dict[str, Any]]:
    done = _render(*RENDERS[render], "--set", f"global.labelValues={values}")
    assert done.returncode == 0, done.stderr[-600:]
    return _comparable(done.stdout)


def _pod_labels(doc: dict[str, Any]) -> dict[str, str] | None:
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    if doc["kind"] == "SandboxTemplate":
        return dict(spec["podTemplate"]["metadata"]["labels"])
    template = spec.get("template") if isinstance(spec, dict) else None
    if doc["kind"] in {"Deployment", "StatefulSet", "Job", "CronJob", "Rollout"} and template:
        return dict((template.get("metadata") or {}).get("labels") or {})
    return None


def _selector(doc: dict[str, Any]) -> dict[str, Any] | None:
    spec = doc.get("spec") or {}
    if doc["kind"] == "Service":
        return spec.get("selector")
    if doc["kind"] in {"Deployment", "StatefulSet", "Rollout", "PodDisruptionBudget"}:
        return (spec.get("selector") or {}).get("matchLabels")
    return None


@pytest.mark.parametrize("render", sorted(RENDERS))
def test_the_former_values_stay_the_default(render: str) -> None:
    done = _render(*RENDERS[render])
    assert done.returncode == 0, done.stderr[-600:]
    default = _comparable(done.stdout)
    assert default == _docs(render, "former")
    names = {labels.get(NAME_LABEL) for d in default if (labels := _pod_labels(d))}
    assert "choregos-api" in names or render == "everything"
    assert not {n for n in names if n and n.startswith("tranibor-")}, "a pod's name moved without its switch"


@pytest.mark.parametrize("render", sorted(RENDERS))
def test_the_switch_moves_the_platform_s_pods_and_recreates_nothing(render: str) -> None:
    former, current = _docs(render, "former"), _docs(render, "tranibor")
    assert [(d["kind"], d["metadata"]["name"]) for d in former] == [
        (d["kind"], d["metadata"]["name"]) for d in current
    ], "the same objects both ways"
    moved = 0
    for before, after in zip(former, current, strict=True):
        name = f"{before['kind']}/{before['metadata']['name']}"
        # A selector is immutable on a Deployment and a StatefulSet: the same both ways.
        assert _selector(before) == _selector(after), name
        if (labels := _pod_labels(after)) is None:
            continue
        old = _pod_labels(before) or {}
        if old.get(NAME_LABEL, "").startswith("choregos-") and labels.get(NAME_LABEL, "").startswith(
            "tranibor-"
        ):
            moved += 1
            assert labels[NAME_LABEL] == "tranibor-" + old[NAME_LABEL].removeprefix("choregos-"), name
        if PART_OF in old and old[PART_OF] == "choregos" and labels.get(PART_OF) == "tranibor":
            moved += 1
    assert moved, "the switch moved nothing"


@pytest.mark.parametrize("values", ["former", "tranibor"])
@pytest.mark.parametrize("render", sorted(RENDERS))
def test_every_selector_still_selects_its_own_pods(render: str, values: str) -> None:
    docs = _docs(render, values)
    pods = {f"{d['kind']}/{d['metadata']['name']}": labels for d in docs if (labels := _pod_labels(d))}
    for doc in docs:
        selector = _selector(doc)
        if not selector or doc["kind"] == "PodDisruptionBudget":
            continue
        selected = [name for name, labels in pods.items() if selector.items() <= labels.items()]
        assert selected, f"{doc['kind']}/{doc['metadata']['name']} selects no pod under {values}"
        if doc["kind"] in {"Deployment", "StatefulSet", "Rollout"}:
            assert selected == [f"{doc['kind']}/{doc['metadata']['name']}"], selected


def test_the_runners_and_what_holds_data_keep_their_values_both_ways() -> None:
    """The runners' labels are the code's (`k8s_job.RUNNER_POD_LABELS`, the template's); objects named
    after the release (PostgreSQL's StatefulSet) keep the selector the former chart gave them."""
    for values in ("former", "tranibor"):
        docs = {f"{d['kind']}/{d['metadata']['name']}": d for d in _docs("all-in-one", values)}
        postgresql = docs["StatefulSet/platform-postgresql"]
        assert postgresql["spec"]["selector"]["matchLabels"][NAME_LABEL] == "choregos-postgresql"
    sandbox = {f"{d['kind']}/{d['metadata']['name']}": d for d in _docs("default", "tranibor")}
    template = sandbox["SandboxTemplate/tranibor-runner"]["spec"]["podTemplate"]["metadata"]["labels"]
    assert template[NAME_LABEL] == "choregos-runner"


def test_a_value_the_chart_does_not_know_is_refused() -> None:
    done = _render("--set", "global.labelValues=both")
    assert done.returncode != 0
    assert "global.labelValues=both: `former` (the default until 0.32.0) or `tranibor`" in done.stderr
