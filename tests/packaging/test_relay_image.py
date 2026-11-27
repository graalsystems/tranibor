"""Is the relay shipped as an image of its own, carrying the relay and nothing else? (S87-44, ADR 0230 §12)

The relay is not one of the platform's workloads: whoever operates a relay runs its image, from its
chart (`charts/choregos-relay`). The image carries the relay and what it imports (the contract), runs
as a user without any capability on two ports above 1024, and starts `choregos-relay serve`.

Building the image is the CI's job (`images`), and the build itself checks that the relay starts and
finds its contract; these tests keep a later edit from undoing the lines that say so.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "relay.Dockerfile"
PACKAGE = "tranibor-sni-relay"
COMMAND = "choregos-relay"
MOUNT = (
    "--mount=type=bind,from=build-ca,target=/mnt/build-ca "
    "--mount=type=bind,source=docker/build-ca.sh,target=/mnt/build-ca.sh"
)


def _stages() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    parts = re.split(r"^FROM \S+ AS ([\w-]+)$", text, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _instructions(stage: str) -> list[str]:
    """The stage's instructions, comments left out and continued lines joined."""
    lines = [line for line in stage.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\\\n\s*", " ", "\n".join(lines)).splitlines()


def test_the_package_has_the_command_the_image_runs() -> None:
    pyproject = ROOT / "packages" / "sni-relay" / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    assert project["name"] == PACKAGE and project["scripts"] == {COMMAND: "tranibor_sni_relay.server:main"}
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["version"] == workspace["project"]["version"], "one version everywhere (`bump_version.py`)"


def test_the_image_carries_the_relay_and_nothing_else_of_the_repository() -> None:
    stages = _stages()
    assert list(stages) == ["build-ca", "builder", "runtime"]
    sync = next(line for line in _instructions(stages["builder"]) if "uv sync" in line)
    for flag in (f"--package {PACKAGE}", "--no-editable", "--frozen", "--no-dev"):
        assert flag in sync, flag
    assert "--all-packages" not in sync
    # Behind a TLS-inspecting proxy, the download trusts the authority given for the build only.
    assert MOUNT in sync and ". /mnt/build-ca.sh" in sync
    runtime = _instructions(stages["runtime"])
    copied = [line for line in runtime if line.startswith("COPY")]
    assert copied == ["COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv"]
    assert not [line for line in runtime if "build-ca" in line], "the shipped stage carries the authority"
    # The build fails when the relay does not start or its contract is missing.
    checks = next(line for line in runtime if line.startswith("RUN ") and f"{COMMAND} --version" in line)
    assert "relay-metadata-line.schema.json" in checks and f"{COMMAND} check" in checks
    assert "test ! -e /app/packages" in checks


def test_the_relay_runs_unprivileged_on_the_ports_its_chart_targets() -> None:
    runtime = _instructions(_stages()["runtime"])
    assert "USER 1000" in runtime
    assert runtime[-2:] == [f'ENTRYPOINT ["/usr/bin/tini", "--", "{COMMAND}"]', 'CMD ["serve"]']
    (expose,) = [line for line in runtime if line.startswith("EXPOSE")]
    exposed = sorted(int(port) for port in expose.split()[1:])
    values = yaml.safe_load((ROOT / "charts/choregos-relay/values.yaml").read_text(encoding="utf-8"))
    assert exposed == sorted(values["ports"].values())
    assert all(port >= 1024 for port in exposed), "a user without any capability cannot bind below 1024"
    env = " ".join(line for line in runtime if line.startswith("ENV"))
    assert f"TRANIBOR_RELAY_SNI_PORT={values['ports']['sni']}" in env
    assert f"TRANIBOR_RELAY_OWN_PORT={values['ports']['own']}" in env
    # Nothing of an operator's is baked in.
    assert "TRANIBOR_RELAY_DOMAIN" not in env and "TRANIBOR_RELAY_RETENTION_DAYS" not in env


def test_the_ci_builds_and_scans_the_image_on_pull_requests() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    built = ci["jobs"]["images"]["strategy"]["matrix"]["include"]
    assert {"image": "relay", "file": "docker/relay.Dockerfile"} in built
    assert any("trivy" in str(step.get("uses", "")) for step in ci["jobs"]["images"]["steps"])
