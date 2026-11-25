"""The platform's commands are named `tranibor-*`, and keep their former `choregos-*` names (ADR 0224 R5).

Each distribution declares the new command and, for the window of ADR 0224 §1, the former one, both on
the same function; the images call the new one (`docker/*.Dockerfile`, checked in their own tests) and
an operator's script or unit that still calls the former one keeps working until the window closes.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: The distribution's `pyproject.toml` -> the commands it must declare, each with its former name.
COMMANDS = {
    "apps/api": {"tranibor-admin": "choregos-admin", "tranibor-api": "choregos-api"},
    "apps/orchestrator": {"tranibor-orchestrator": "choregos-orchestrator"},
    "packages/runner": {"tranibor-runner": "choregos-runner"},
    "packages/outpost": {"tranibor-outpost": "choregos-outpost"},
    "packages/tools-mcp": {"tranibor-tools": "choregos-tools"},
    "packages/cli": {"tranibor": "choregos"},
}


def _scripts(directory: str) -> dict[str, str]:
    with (ROOT / directory / "pyproject.toml").open("rb") as handle:
        return dict(tomllib.load(handle)["project"]["scripts"])


@pytest.mark.parametrize("directory", sorted(COMMANDS))
def test_each_command_has_its_tranibor_name_and_keeps_its_former_one(directory: str) -> None:
    scripts = _scripts(directory)
    for current, former in COMMANDS[directory].items():
        assert current in scripts, f"{directory} declares no `{current}`"
        assert scripts.get(former) == scripts[current], f"`{former}` is not an alias of `{current}`"


def test_the_installed_environment_has_both_names() -> None:
    installed = {entry.name: entry.value for entry in metadata.entry_points(group="console_scripts")}
    for commands in COMMANDS.values():
        for current, former in commands.items():
            assert current in installed, f"`{current}` is not installed: `uv sync`"
            assert installed.get(former) == installed[current]
