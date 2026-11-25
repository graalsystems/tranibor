"""ADR 0222: `global.runner.agentIsolation: process` in staging or prod is refused at the render unless the
operator accepts it by name; the acceptance reaches every pod that judges it; gVisor stays an opt-in."""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"
ACCEPT = (
    "CHOREGOS_AGENT_ISOLATION"  # rendered as the chart's `CHOREGOS_*`, adopted as `TRANIBOR_*` (ADR 0176)
)

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "choregos", str(CHART), "--namespace", "choregos"]
    for value in sets:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)


def _envs(stdout: str, name: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for doc in yaml.safe_load_all(stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env", []):
                if env["name"] == name:
                    found.setdefault(doc["metadata"]["name"], []).append(env.get("value", ""))
    return found


@needs_helm
@pytest.mark.parametrize("environment", ["staging", "prod"])
@pytest.mark.parametrize("executor", ["tekton", "aca", "k8s_job", "agent_sandbox"])
def test_process_in_production_is_refused_naming_the_opt_out(environment: str, executor: str) -> None:
    result = _render(
        "global.runner.agentIsolation=process",
        f"global.environment={environment}",
        f"global.runner.executor={executor}",
    )
    assert result.returncode != 0
    for wanted in (
        "global.runner.acceptUnisolatedAgent",
        "TRANIBOR_AGENT_ISOLATION=none-accepted",
        "ADR 0222",
    ):
        assert wanted in result.stderr


@needs_helm
def test_the_named_opt_out_renders_and_reaches_the_api_and_the_workers() -> None:
    result = _render(
        "global.runner.agentIsolation=process",
        "global.environment=prod",
        "global.runner.executor=tekton",
        "global.runner.acceptUnisolatedAgent=true",
    )
    assert result.returncode == 0, result.stderr
    seen = _envs(result.stdout, ACCEPT)
    assert {"choregos-api", "choregos-orchestrator-executor"} <= set(seen), seen
    assert all(values == ["none-accepted"] for values in seen.values())


@needs_helm
@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_the_default_isolates_and_needs_no_opt_out(environment: str) -> None:
    result = _render(f"global.environment={environment}")
    assert result.returncode == 0, result.stderr
    assert _envs(result.stdout, ACCEPT) == {}, "nothing is accepted by default"
    seen = _envs(result.stdout, "CHOREGOS_RUNNER_AGENT_ISOLATION")
    assert {value for values in seen.values() for value in values} == {"container"}


@needs_helm
def test_the_opt_out_means_nothing_under_container_and_is_not_rendered() -> None:
    result = _render("global.environment=prod", "global.runner.acceptUnisolatedAgent=true")
    assert result.returncode == 0, result.stderr
    assert _envs(result.stdout, ACCEPT) == {}


@needs_helm
def test_process_outside_production_still_renders_without_it() -> None:
    result = _render("global.runner.agentIsolation=process", "global.environment=dev")
    assert result.returncode == 0, result.stderr
    assert _envs(result.stdout, ACCEPT) == {}


@needs_helm
def test_gvisor_is_an_opt_in_and_the_default_names_no_runtime_class() -> None:
    """ADR 0222: no default `runtimeClassName`, and nothing asks the cluster for the class (`lookup` would
    answer differently under `helm template`, Argo CD and `helm install`)."""
    result = _render()
    assert result.returncode == 0, result.stderr
    assert "runtimeClassName" not in result.stdout
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in CHART.rglob("*")
        if path.is_file() and path.suffix in {".yaml", ".tpl"} and "templates" in path.parts
    )
    assert "RuntimeClass" not in "".join(line for line in templates.splitlines() if "lookup" in line), (
        "a RuntimeClass lookup would make the default depend on the renderer"
    )
    opted = _render("global.runner.runtimeClass=gvisor")
    assert opted.returncode == 0, opted.stderr
    classes = {
        doc["spec"]["podTemplate"]["spec"].get("runtimeClassName")
        for doc in yaml.safe_load_all(opted.stdout)
        if doc and doc.get("kind") == "SandboxTemplate"
    }
    assert classes == {"gvisor"}
