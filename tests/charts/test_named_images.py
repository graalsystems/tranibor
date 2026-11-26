"""The images an operator names reach the API and every orchestrator worker (ADR 0056, ADR 0063)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
DIGEST = "sha256:" + "a" * 64

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _envs(*overrides: str) -> dict[str, dict[str, str]]:
    """The plain env of the API and of each orchestrator worker, by deployment name."""
    command = ["helm", "template", "choregos", str(CHART)]
    for override in overrides:
        command += ["--set", override]
    rendered = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rendered.returncode == 0, rendered.stderr
    documents: list[dict[str, Any]] = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    out: dict[str, dict[str, str]] = {}
    for deployment in (d for d in documents if d.get("kind") == "Deployment"):
        name = deployment["metadata"]["name"]
        if name != "tranibor-api" and not name.startswith("tranibor-orchestrator"):
            continue
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        out[name] = {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
    return out


def test_nothing_is_named_by_default_and_digests_are_required() -> None:
    envs = _envs()
    assert "tranibor-api" in envs and len(envs) > 1
    for name, env in envs.items():
        assert "TRANIBOR_JOB_IMAGES" not in env and "TRANIBOR_AGENT_IMAGES" not in env, name
        assert env["TRANIBOR_REQUIRE_IMAGE_DIGEST"] == "true", name


def test_the_named_images_reach_the_api_and_every_worker() -> None:
    envs = _envs(
        f"global.images.agents.triager=registry.example/agents/triager@{DIGEST}",
        f"global.images.jobs.quant=registry.example/quant@{DIGEST}",
    )
    for name, env in envs.items():
        assert json.loads(env["TRANIBOR_AGENT_IMAGES"]) == {
            "triager": f"registry.example/agents/triager@{DIGEST}"
        }, name
        assert json.loads(env["TRANIBOR_JOB_IMAGES"]) == {"quant": f"registry.example/quant@{DIGEST}"}, name


def test_a_bench_can_turn_the_digest_rule_off() -> None:
    envs = _envs("global.images.requireDigest=false")
    assert all(env["TRANIBOR_REQUIRE_IMAGE_DIGEST"] == "false" for env in envs.values())


def test_hosted_registries_and_the_signature_mode_reach_the_api_only() -> None:
    default = _envs()["tranibor-api"]
    assert "TRANIBOR_HOSTED_REGISTRIES" not in default
    assert default["TRANIBOR_HOSTED_COSIGN"] == "off"
    envs = _envs(
        "global.images.hostedRegistries={ghcr.io/acme,registry.internal}",
        "global.images.hostedCosign=enforce",
    )
    assert json.loads(envs["tranibor-api"]["TRANIBOR_HOSTED_REGISTRIES"]) == [
        "ghcr.io/acme",
        "registry.internal",
    ]
    assert envs["tranibor-api"]["TRANIBOR_HOSTED_COSIGN"] == "enforce"
    assert all("TRANIBOR_HOSTED_COSIGN" not in env for name, env in envs.items() if name != "tranibor-api")


def test_the_build_repository_and_the_builder_reach_every_worker_not_the_api() -> None:
    """S41-08: the executor worker builds and pushes a hosted zip's image; the API only starts it."""
    default = _envs()
    workers = {name: env for name, env in default.items() if name != "tranibor-api"}
    assert all("TRANIBOR_HOSTED_BUILD_REPOSITORY" not in env for env in workers.values())
    assert all(env["TRANIBOR_HOSTED_BUILDER"] == "buildkit" for env in workers.values())
    envs = _envs(
        "global.images.buildRegistry=registry.internal/choregos-builds",
        "global.images.hostedBuilder=local_docker",
        "global.images.buildkitAddress=tcp://buildkitd.builds:1234",
    )
    for name, env in envs.items():
        if name == "tranibor-api":
            assert "TRANIBOR_HOSTED_BUILD_REPOSITORY" not in env
            continue
        assert env["TRANIBOR_HOSTED_BUILD_REPOSITORY"] == "registry.internal/choregos-builds", name
        assert env["TRANIBOR_HOSTED_BUILDER"] == "local_docker", name
        assert env["TRANIBOR_BUILDKIT_ADDRESS"] == "tcp://buildkitd.builds:1234", name


def _rendered_for(kube_version: str, *overrides: str) -> dict[str, str]:
    command = ["helm", "template", "choregos", str(CHART), "--kube-version", kube_version]
    for override in overrides:
        command += ["--set", override]
    rendered = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rendered.returncode == 0, rendered.stderr
    for document in (d for d in yaml.safe_load_all(rendered.stdout) if d):
        if document.get("kind") == "Deployment" and document["metadata"]["name"] == "tranibor-api":
            container = document["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
    raise AssertionError("no API deployment")


def test_the_cluster_version_reaches_the_api_from_the_cluster_or_the_operator() -> None:
    """S41-16: the API refuses a hosted version below 1.29 from the version the chart renders for.
    Since S87-16 the chart itself refuses to render for a cluster below 1.29
    (`test_cluster_assumptions.py`): the version reaches the API from a cluster it accepts."""
    assert _rendered_for("1.29.0")["TRANIBOR_CLUSTER_VERSION"] == "v1.29.0"
    assert _rendered_for("1.30.1")["TRANIBOR_CLUSTER_VERSION"] == "v1.30.1"
    overridden = _rendered_for("1.33.0", "global.kubernetesVersion=v1.28.9-eks-abc")
    assert overridden["TRANIBOR_CLUSTER_VERSION"] == "v1.28.9-eks-abc"
    assert all(
        "TRANIBOR_CLUSTER_VERSION" not in env for name, env in _envs().items() if name != "tranibor-api"
    )
