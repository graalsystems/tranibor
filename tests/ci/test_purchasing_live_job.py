"""The nightly proof of the purchasing server against a real Odoo cannot quietly stop being one (S85-14).

Four ways it could: the job is dropped from the schedule or from the alert's `needs`, a service image floats
(a night that runs another Odoo than the one that was read is no proof), the wait for Odoo is open-ended or
falls through to a green run, or a live test learns to skip (finding TST-3). These checks read the workflow
and the module, and need no Docker.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
NIGHTLY = ROOT / ".github" / "workflows" / "nightly.yml"
LIVE = ROOT / "tests" / "live" / "purchasing"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
SKIPS = {"skip", "skipif", "xfail", "importorskip", "require"}


def _job() -> dict[str, Any]:
    return dict(yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]["purchasing-live"])


def _steps_running(fragment: str) -> list[str]:
    return [str(step["run"]) for step in _job()["steps"] if fragment in str(step.get("run", ""))]


def test_the_job_exists_and_a_failure_of_it_reaches_the_alert() -> None:
    document = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    assert "purchasing-live" in document["jobs"]
    assert "purchasing-live" in document["jobs"]["alerte"]["needs"], "the night's alert must wait for it"
    assert _job().get("continue-on-error", False) is False


def test_every_service_image_is_pinned_by_digest() -> None:
    job = _job()
    matrix = {item["image"] for item in job["strategy"]["matrix"]["include"]}
    assert len(matrix) >= 2, "Odoo 19 and Odoo 20 are both served (PROFILE.md): both are played"
    images: list[str] = []
    for name, service in job["services"].items():
        image = str(service["image"])
        if image == "${{ matrix.image }}":
            images += sorted(matrix)
        else:
            images.append(image)
        assert "image" in service, name
    assert {"db", "odoo"} <= set(job["services"]), "Odoo and its database are the job's services"
    assert images, "no service image"
    for image in [*images, str(job["env"]["TLS_IMAGE"])]:
        assert DIGEST.search(image), f"{image} floats: pin it by digest"
    assert any(i.startswith("odoo:19") for i in matrix) and any(i.startswith("odoo:20") for i in matrix)


def test_the_server_image_is_the_published_one_pinned_by_digest() -> None:
    """The image the agents repository published, by its registry digest: never a tag alone, never the
    placeholder of zeros the job carried before procurement 0.2.0 was published."""
    image = str(_job()["env"]["PURCHASING_IMAGE"])
    assert image.startswith("ghcr.io/graalsystems/tranibor-agents/purchasing-mcp:"), image
    assert DIGEST.search(image), "the server image must be pinned by digest"
    assert "sha256:" + "0" * 64 not in image, (
        "a placeholder digest is never pullable: write the published one"
    )
    assert "latest" not in image, "a night that runs whatever was last built proves nothing about a release"


def test_the_wait_for_odoo_is_bounded_and_fails_instead_of_falling_through() -> None:
    waits = _steps_running("/web/health")
    assert waits, "no step waits for Odoo"
    first = waits[0]
    assert re.search(r"for \w+ in \$\(seq 1 \d+\)", first), "the wait must be a bounded loop"
    assert first.rstrip().endswith("exit 1"), "after the last try the step must fail, not carry on"
    assert "exit 0" in first
    for run in (*waits, *_steps_running("/healthz")):
        assert "|| true" not in run.split("docker logs")[0], "a wait must not swallow its own failure"


def test_the_pytest_step_runs_the_module_with_its_own_marker_and_no_other_marker_hides_it() -> None:
    runs = _steps_running("pytest")
    assert runs == ["uv run pytest tests/live/purchasing -q -m purchasing_live -rs"], runs
    markers = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8"))
    live_run = " ".join(str(s.get("run", "")) for s in markers["jobs"]["live"]["steps"])
    assert "-m live" in live_run, (
        "the existing `live` job selects `live`: the module's marker is not that one"
    )
    assert "purchasing_live" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_live_module_never_skips() -> None:
    sources = sorted(LIVE.glob("*.py"))
    assert [p.name for p in sources if p.name.startswith("test_")] == ["test_purchasing_live.py"]
    offending: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
            if isinstance(node, ast.Attribute | ast.Name) and name in SKIPS:
                offending.append(f"{path.name}:{node.lineno}: {name}")
            if isinstance(node, ast.alias) and node.name in SKIPS:
                offending.append(f"{path.name}: imports {node.name}")
    assert not offending, f"the purchasing live proof must fail, never skip: {offending}"
    text = (LIVE / "test_purchasing_live.py").read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.purchasing_live" in text
    assert "tests/live/conftest" not in text


def test_every_operation_of_the_profile_is_asserted() -> None:
    """The module's last test compares the played set with the eleven; this keeps the eleven honest."""
    support = ast.parse((LIVE / "support.py").read_text(encoding="utf-8"))
    operations = {
        node.value
        for assign in support.body
        if isinstance(assign, ast.Assign)
        and any(getattr(t, "id", "") == "OPERATIONS" for t in assign.targets)
        for node in ast.walk(assign.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert len(operations) == 11
    text = (LIVE / "test_purchasing_live.py").read_text(encoding="utf-8")
    for operation in sorted(operations):
        assert f'"{operation}"' in text, f"no live test plays {operation}"
    for code in ("not_supported", "not_found", "conflict", "refused"):
        assert f'"{code}"' in text, f"the live module never asserts `{code}`"
