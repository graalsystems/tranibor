"""Every workspace member reaches the API image's builder before its `uv sync --all-packages`.

`uv sync --frozen --all-packages` resolves the whole workspace: a member whose directory the builder does
not hold stops the build ("Distribution not found at: file:///app/tests/testkit"). That is how #2133,
moving the test kit from `packages/` to `tests/testkit`, broke the API and worker images and the local
install on main. A member the image does not install is copied for its metadata only and named in a
`--no-install-package`.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "api.Dockerfile"


def _members() -> list[str]:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"][
        "workspace"
    ]
    excluded = set(workspace.get("exclude", []))
    found: list[str] = []
    for pattern in workspace["members"]:
        for path in sorted(ROOT.glob(pattern)):
            relative = path.relative_to(ROOT).as_posix()
            if relative not in excluded and (path / "pyproject.toml").is_file():
                found.append(relative)
    return found


def _builder() -> list[str]:
    """The builder stage's instructions up to its `uv sync`, continuation lines joined."""
    text = re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text(encoding="utf-8"))
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    sync = next(i for i, line in enumerate(lines) if line.startswith("RUN uv sync"))
    return lines[: sync + 1]


def _copied(builder: list[str]) -> set[str]:
    """The paths the builder copies from the build context (not `--from=` another image)."""
    paths: set[str] = set()
    for line in builder:
        if line.startswith("COPY ") and "--from=" not in line:
            *sources, _target = line.split()[1:]
            paths.update(source.rstrip("/") for source in sources)
    return paths


def test_every_workspace_member_reaches_the_builder_before_the_sync() -> None:
    copied = _copied(_builder())
    missing = [
        member
        for member in _members()
        if not any(
            member == path or member.startswith(f"{path}/") or path == f"{member}/pyproject.toml"
            for path in copied
        )
    ]
    assert len(_members()) > 10, "the workspace's members were not read"
    assert not missing, f"workspace members the API image's builder lacks (uv sync fails): {missing}"


def test_the_test_kit_is_read_for_its_metadata_and_not_installed() -> None:
    sync = _builder()[-1]
    assert "--all-packages" in sync
    assert "--no-install-package tranibor-testkit" in sync, "the API image would ship the suites' test kit"
    assert "tests/testkit/pyproject.toml" in _copied(_builder())
