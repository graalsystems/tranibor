"""Can the images the local install builds be built behind a TLS-inspecting proxy?

Such a proxy re-signs every certificate with its own authority, which no base image trusts: on
2026-11-25 the load test (`docs/load-testing.md`) could not build the images at all (`uv`: `invalid
peer certificate: UnknownIssuer`, corepack: `self-signed certificate in certificate chain`) and had to
run the published 0.29.4 images instead of the commit it measured. The seam: a build context
`build-ca`, empty by default, that `docker/build-ca.sh` adds to each downloading step's trust.

What this guards: every step that downloads mounts it; no stage that ends up in an image does (the
authority must never ship); the script trusts the files it is given and nothing when it is given
none; `compose.yaml` hands the context, the proxy and the build network to every image it builds.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"
SCRIPT = DOCKER / "build-ca.sh"
#: The Dockerfiles `compose.yaml` builds.
BUILT_BY_COMPOSE = ("api.Dockerfile", "web.Dockerfile", "laya.Dockerfile")
#: What a step that fetches from the network looks like in those files.
DOWNLOADS = re.compile(r"\b(uv sync|uv pip install|git clone|curl |pnpm install|pnpm build|build_router)")
MOUNT = (
    "--mount=type=bind,from=build-ca,target=/mnt/build-ca "
    "--mount=type=bind,source=docker/build-ca.sh,target=/mnt/build-ca.sh"
)
STAGE = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.M | re.I)


def _runs(text: str) -> list[tuple[str, str]]:
    """(stage, RUN instruction with its continuation lines joined) of a Dockerfile."""
    runs: list[tuple[str, str]] = []
    stage = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        found = STAGE.match(line)
        if found:
            stage = found.group(2) or found.group(1)
        if line.startswith("RUN "):
            parts = [line]
            while parts[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                if not lines[i].lstrip().startswith("#"):
                    parts.append(lines[i])
            runs.append((stage, " ".join(p.rstrip("\\").strip() for p in parts)))
        i += 1
    return runs


def _shipped_stages(text: str) -> set[str]:
    """The stages an image is made of: the target stages and those they start FROM, transitively."""
    stages = {m.group(2): m.group(1) for m in STAGE.finditer(text) if m.group(2)}
    sources = set(re.findall(r"(?:COPY\s+--from=|--mount=\S*?\bfrom=)([^\s,]+)", text))
    bases = {base for base in stages.values() if base in stages}
    targets = {name for name in stages if name not in sources and name not in bases}
    shipped: set[str] = set()
    for target in targets:
        name = target
        while name in stages and name not in shipped:
            shipped.add(name)
            name = stages[name]
    return shipped


@pytest.mark.parametrize("dockerfile", BUILT_BY_COMPOSE)
def test_every_download_mounts_the_extra_authority(dockerfile: str) -> None:
    text = (DOCKER / dockerfile).read_text(encoding="utf-8")
    assert re.search(r"^FROM scratch AS build-ca$", text, re.M), "the empty default context is missing"
    downloads = [(stage, run) for stage, run in _runs(text) if DOWNLOADS.search(run)]
    assert downloads, "no download found: the pattern no longer matches the file"
    missing = [
        (stage, run[:90]) for stage, run in downloads if MOUNT not in run or ". /mnt/build-ca.sh" not in run
    ]
    assert not missing, missing


@pytest.mark.parametrize("dockerfile", BUILT_BY_COMPOSE)
def test_no_shipped_stage_carries_the_authority(dockerfile: str) -> None:
    text = (DOCKER / dockerfile).read_text(encoding="utf-8")
    shipped = _shipped_stages(text)
    assert shipped, "no shipped stage found"
    leaking = [stage for stage, run in _runs(text) if stage in shipped and "build-ca" in run]
    assert not leaking, leaking


def test_compose_hands_the_context_the_proxy_and_the_network_to_every_build() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    built = {name: svc["build"] for name, svc in services.items() if isinstance(svc.get("build"), dict)}
    assert {"api", "worker", "web", "laya"} <= set(built), set(built)
    for name, build in built.items():
        context = build.get("additional_contexts", {}).get("build-ca")
        assert context == "${TRANIBOR_BUILD_CA_DIR:-./docker/build-ca}", name
        assert build.get("network") == "${TRANIBOR_BUILD_NETWORK:-default}", name
        assert {"HTTPS_PROXY", "NO_PROXY"} <= set(build.get("args", {})), name
    assert (DOCKER / "build-ca").is_dir()
    committed = [p for p in (DOCKER / "build-ca").iterdir() if p.suffix in {".pem", ".crt"}]
    assert not committed, "an authority was committed"


def _source(mounted: pathlib.Path, tmp: pathlib.Path) -> dict[str, str]:
    """Runs the script against `mounted` as its `/mnt/build-ca`, and returns what it exported."""
    script = SCRIPT.read_text(encoding="utf-8").replace("/tmp/", f"{tmp}/")
    script = script.replace("/mnt/build-ca", str(mounted))
    copy = tmp / "build-ca.sh"
    copy.write_text(script, encoding="utf-8")
    sh = shutil.which("sh")
    assert sh
    out = subprocess.run(
        [sh, "-c", f". {copy} && env"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_the_script_trusts_the_given_authority(tmp_path: pathlib.Path) -> None:
    mounted = tmp_path / "ca"
    mounted.mkdir()
    (mounted / "proxy.crt").write_text("-----BEGIN CERTIFICATE-----\nproxy\n-----END CERTIFICATE-----\n")
    env = _source(mounted, tmp_path)
    bundle = tmp_path / "build-ca.pem"
    exported = (
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    )
    for var in exported:
        assert env.get(var) == str(bundle), var
    assert "proxy" in bundle.read_text()


def test_the_script_does_nothing_without_an_authority(tmp_path: pathlib.Path) -> None:
    mounted = tmp_path / "ca"
    mounted.mkdir()
    (mounted / "README.md").write_text("empty on purpose")
    env = _source(mounted, tmp_path)
    assert not {"SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"} & set(env)
    assert not (tmp_path / "build-ca.pem").exists()
