# SPDX-License-Identifier: Apache-2.0
"""One image and one chart, one name (ADR 0176 R3, S75-03 then S75-05).

A release publishes every image as `<registry>/tranibor-<component>` and every chart as `tranibor`
(`tranibor-outpost`), packaged from its directory. The former names, `choregos-*`, were published beside
them for the deprecation window (0.27.14 to 0.29.4) and stopped with step (e): nothing pushes them any
more, by release or by commit. The name comes from the publishing identity (`image_prefix`, ADR 0156).
What a deployment must be able to rely on:

* the chart renders the same objects under its former name and its current one, so a deployment that
  vendored `choregos` moves to `tranibor` by a change of address and nothing else;
* the release packages and pushes each chart under its one name, and builds, tags and signs each image
  under its one name; `main` does the same with its per-commit images;
* the chart's defaults, and the orchestrator's, name the images the release publishes;
* no file but history names a former image or chart, except the reference admission policy, which
  still admits them (the policies drop them last) and does not take the plugins' images
  (`<registry>/tranibor-agents/...`) for the core's.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import pathlib
import re
import shutil
import subprocess
import tarfile
from typing import Any

import pytest
import yaml
from tranibor_core.publishing import CHARTS, IMAGE_COMPONENTS, PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
# The tool the release runs (`tests/tools/conftest.py` puts the repository on the path for its own
# directory only).
_SPEC = importlib.util.spec_from_file_location("package_charts", ROOT / "tools/package_charts.py")
assert _SPEC is not None and _SPEC.loader is not None
package_charts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(package_charts)
_SPEC = importlib.util.spec_from_file_location("publishing_identity", ROOT / "tools/publishing_identity.py")
assert _SPEC is not None and _SPEC.loader is not None
publishing_identity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publishing_identity)
#: What records the past (`docs/adr/`, `docs/plan/`, trials, …): it says what was true when written.
HISTORY: tuple[str, ...] = publishing_identity.HISTORY
RELEASE = ROOT / ".github/workflows/release.yml"
#: The prefix the images and charts were published under until 0.29.4 (ADR 0176 R3, step (e)).
FORMER = "choregos"
POLICY = ROOT / "infra/policies/images.yaml"
#: The values each chart is rendered with: the outpost's chart refuses to render without its own, and
#: its example names the objects itself (`fullnameOverride`), which would hide the default naming.
VALUES = {
    "": [],
    "-outpost": [
        "-f",
        str(ROOT / "charts/tranibor-outpost/values/example.yaml"),
        "--set",
        "fullnameOverride=",
    ],
}

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(chart: pathlib.Path, *arguments: str) -> list[dict[str, Any]]:
    command = ["helm", "template", "platform", str(chart), *arguments]
    output = subprocess.run(command, capture_output=True, text=True, check=True, timeout=180)
    # The `# Source: <chart>/templates/...` comments name the chart; the objects must not.
    docs = [doc for doc in yaml.safe_load_all(output.stdout) if doc]
    for doc in docs:
        # The master key of Temporal's payload codec is drawn at random on every render (ADR 0220): two
        # renders differ by it and by nothing else.
        if doc["kind"] == "Secret" and doc["metadata"]["name"].endswith("-temporal-codec"):
            doc["stringData"] = dict.fromkeys(doc["stringData"], "<drawn>")
    return docs


def _pod_images(docs: list[dict[str, Any]]) -> set[str]:
    images: set[str] = set()
    for doc in docs:
        spec = doc.get("spec") or {}
        pod = (spec.get("jobTemplate", {}).get("spec") or spec).get("template", {}).get("spec")
        if not pod:
            continue
        for container in pod.get("initContainers", []) + pod.get("containers", []):
            images.add(container["image"])
    return images


# ───────────────────────────── the charts ─────────────────────────────


@helm
@pytest.mark.parametrize("suffix", sorted(CHARTS))
def test_the_former_name_of_a_chart_renders_the_same_objects(suffix: str, tmp_path: pathlib.Path) -> None:
    """A deployment that vendored `choregos` moves to `tranibor` and its cluster sees no change."""
    current, former = PUBLISHING.chart_name(suffix), f"{FORMER}{suffix}"
    renders = {
        name: _render(package_charts.renamed(ROOT / CHARTS[suffix], name, tmp_path), *VALUES[suffix])
        for name in (current, former)
    }
    assert renders[current], f"{current} renders nothing"
    assert renders[current] == renders[former]


@helm
def test_the_release_packages_each_chart_under_its_one_name(tmp_path: pathlib.Path) -> None:
    archives = package_charts.package("9.9.9", tmp_path)
    names = [PUBLISHING.chart_name(suffix) for suffix in CHARTS]
    assert names == ["tranibor", "tranibor-outpost"]
    assert archives == [tmp_path / f"{name}-9.9.9.tgz" for name in names]
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path in archives)
    for name, archive in zip(names, archives, strict=True):
        with tarfile.open(archive) as bundle:
            member = bundle.extractfile(f"{name}/Chart.yaml")
            assert member is not None
            chart = yaml.safe_load(member.read())
        assert (chart["name"], chart["version"], chart["appVersion"]) == (name, "9.9.9", "9.9.9")


# ───────────────────────────── the images ─────────────────────────────


def test_the_release_builds_tags_and_signs_each_image_under_its_one_name() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    release = yaml.safe_load(text)
    images, manifest = release["jobs"]["images"], release["jobs"]["manifest"]
    # Every image the identity knows is built and signed, and no other (quant is a command, #1847).
    assert images["strategy"]["matrix"]["image"] == list(IMAGE_COMPONENTS)
    assert manifest["strategy"]["matrix"]["image"] == list(IMAGE_COMPONENTS)
    current = PUBLISHING.image(f"{PUBLISHING.image_prefix}-${{{{ matrix.image }}}}")
    assert images["env"] == {"IMAGE": current}
    assert manifest["env"] == {"IMAGE": current}
    meta = next(step for step in manifest["steps"] if step.get("id") == "meta")
    assert meta["with"]["images"].strip() == "${{ env.IMAGE }}"
    runs = "\n".join(str(step.get("run", "")) for step in manifest["steps"])
    assert '$(printf "${IMAGE}@sha256:%s " *)' in runs
    assert [line.strip() for line in runs.splitlines() if "cosign sign" in line] == [
        'cosign sign --yes "${IMAGE}@${digest}"'
    ]
    # Step (e): the former names are pushed by no job of the release, images or charts.
    for former in _former_names():
        assert former not in text, f"the release still names {former}"


def test_the_per_commit_images_of_main_carry_one_name_and_are_signed() -> None:
    """`main` pushes each commit's image under `tranibor-*` only, and signs it."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    build = workflow.split("docker/build-push-action", 1)[1].split("Trivy scan", 1)[0]
    signing = workflow.split("- name: keyless signature", 1)[1].split("local-install:", 1)[0]
    tags = [line.strip() for line in build.split("tags: |", 1)[1].split("build-args:", 1)[0].splitlines()]
    tags = [tag for tag in tags if tag and not tag.startswith("#")]
    prefix = PUBLISHING.image(f"{PUBLISHING.image_prefix}-")
    assert tags == [prefix + "${{ matrix.image }}:${{ github.sha }}"]
    assert prefix + "${{ matrix.image }}@${{ steps.build.outputs.digest }}" in signing
    for former in _former_names():
        assert former not in workflow, f"main still pushes {former}"


def _former_names() -> list[str]:
    """The former names under the registry: an image (`<registry>/choregos-<component>`, or before a
    glob or an expression) and a chart (`<registry>/charts/choregos[-outpost]`)."""
    return [PUBLISHING.image(f"{FORMER}-"), PUBLISHING.image(f"charts/{FORMER}")]


def _verify_entries() -> list[dict[str, Any]]:
    (rule,) = yaml.safe_load(POLICY.read_text(encoding="utf-8"))["spec"]["rules"]
    entries: list[dict[str, Any]] = rule["verifyImages"]
    return entries


def _admitting(image: str) -> list[set[str]]:
    """The signers each `verifyImages` entry that takes `image` demands (Kyverno's `*` crosses `/`)."""
    return [
        {entry["keyless"]["subject"] for attestor in item["attestors"] for entry in attestor["entries"]}
        for item in _verify_entries()
        if any(fnmatch.fnmatchcase(image, pattern) for pattern in item["imageReferences"])
    ]


def test_the_policy_admits_each_image_with_the_release_signature() -> None:
    signer = f"{PUBLISHING.source_repository}/.github/workflows/*"
    for component in IMAGE_COMPONENTS:
        name = PUBLISHING.image_name(component)
        for reference in (f"{name}:9.9.9", f"{name}@sha256:{'0' * 64}"):
            assert _admitting(reference) == [{signer}], reference
        # Still admitted, no longer published: a pod of 0.29.4 or before is not refused by this policy.
        # The policies drop the former names last, after the first release without them (S75-05).
        assert _admitting(PUBLISHING.image(f"{FORMER}-{component}:0.29.4")) == [{signer}]
    plugin = PUBLISHING.image(f"{PUBLISHING.image_prefix}-agents/hello-mcp@sha256:{'0' * 64}")
    assert _admitting(plugin) == [], "a plugin server would need the core's signature"


@helm
def test_the_chart_s_defaults_name_the_published_images_and_the_policy_admits_them() -> None:
    """Rendered with its defaults, the chart pulls the core's images under the one published name."""
    enabled = ["--set=choregos-tools.enabled=true", "--set=choregos-laya.enabled=true"]
    rendered = _pod_images(_render(ROOT / CHARTS[""], *enabled))
    core = {image for image in rendered if image.startswith(PUBLISHING.registry + "/")}
    former = {image for image in core if image.startswith(PUBLISHING.image(f"{FORMER}-"))}
    assert not former, f"the chart's defaults still name {sorted(former)}"
    for component in ("api", "web", "orchestrator"):
        assert any(image.startswith(PUBLISHING.image_name(component) + ":") for image in core), core
    for image in core:
        if any(image.startswith(PUBLISHING.image_name(c)) for c in IMAGE_COMPONENTS):
            assert _admitting(image) == [{f"{PUBLISHING.source_repository}/.github/workflows/*"}], image


def test_every_default_image_of_the_charts_and_the_orchestrator_is_a_published_name() -> None:
    """The values files and the orchestrator's settings: a default still naming `choregos-<component>`
    would pull an image no release after 0.29.4 pushes."""
    from tranibor_orchestrator.config import OrchestratorSettings

    pattern = re.compile(
        rf"^\s*(?:repository|image):\s*\"?(\w+)-({'|'.join(IMAGE_COMPONENTS)})\b", re.MULTILINE
    )
    seen = 0
    for values in sorted((ROOT / "charts").rglob("values.yaml")):
        for prefix, component in pattern.findall(values.read_text(encoding="utf-8")):
            seen += 1
            assert prefix == PUBLISHING.image_prefix, f"{values.relative_to(ROOT)}: {prefix}-{component}"
    assert seen >= 10, f"the defaults moved: {seen} image defaults found"
    default = OrchestratorSettings.model_fields["runner_image"].default
    assert default.startswith(PUBLISHING.image_name("runner") + ":"), default


def test_nothing_but_history_names_a_former_image_or_chart() -> None:
    """Step (e): a tracked file naming `<registry>/choregos-<component>` or `<registry>/charts/choregos`
    is history (an ADR, the plan, a trial) or the reference policy that still admits them."""
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    names = re.compile(
        re.escape(PUBLISHING.registry)
        + rf"/(?:{FORMER}-(?:{'|'.join(IMAGE_COMPONENTS)}|\*|\$)|charts/{FORMER}\b)"
    )
    allowed = {"infra/policies/images.yaml", "tests/charts/test_chart_names.py"}
    offenders = []
    for name in filter(None, files):
        if name in allowed or name.startswith(HISTORY):
            continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [
            f"{name}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}" for m in names.finditer(text)
        ]
    assert not offenders, "the former names are no longer published:\n" + "\n".join(offenders)
