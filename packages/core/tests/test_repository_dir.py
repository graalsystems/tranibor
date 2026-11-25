"""The repository's directory is `.tranibor/`, `.choregos/` read when it is absent (ADR 0226 R5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tranibor_core.policy import PRESETS_DIR, load_preset
from tranibor_core.repository_dir import DIRECTORIES, DIRECTORY, FORMER_DIRECTORY, repository_file

ROOT = Path(__file__).resolve().parents[3]


def test_the_names() -> None:
    assert (DIRECTORY, FORMER_DIRECTORY) == (".tranibor", ".choregos")
    assert DIRECTORIES == (DIRECTORY, FORMER_DIRECTORY)


def test_a_file_is_found_in_the_new_directory_then_in_the_former(tmp_path: Path) -> None:
    assert repository_file(tmp_path, "policy.yaml") == tmp_path / ".tranibor" / "policy.yaml"
    (tmp_path / ".choregos").mkdir()
    (tmp_path / ".choregos" / "policy.yaml").write_text("former", encoding="utf-8")
    assert repository_file(tmp_path, "policy.yaml") == tmp_path / ".choregos" / "policy.yaml"
    (tmp_path / ".tranibor").mkdir()
    (tmp_path / ".tranibor" / "policy.yaml").write_text("current", encoding="utf-8")
    assert repository_file(tmp_path, "policy.yaml").read_text(encoding="utf-8") == "current"


@pytest.mark.parametrize("preset", sorted(path.stem for path in PRESETS_DIR.glob("*.yaml")))
def test_every_preset_keeps_agents_out_of_both_directories(preset: str) -> None:
    denied = load_preset(preset).scope.deny_paths
    assert all(f"{directory}/**" in denied for directory in DIRECTORIES), denied


@pytest.mark.parametrize(
    "policy", sorted(str(p.relative_to(ROOT)) for p in (ROOT / "templates").glob("*/policy.yaml"))
)
def test_every_template_policy_keeps_agents_out_of_both_directories(policy: str) -> None:
    denied = yaml.safe_load((ROOT / policy).read_text(encoding="utf-8"))["scope"]["deny_paths"]
    assert all(f"{directory}/**" in denied for directory in DIRECTORIES), denied


def test_a_template_s_scaffold_writes_the_new_directory() -> None:
    scaffolds = sorted((ROOT / "templates").glob("*/scaffold"))
    assert scaffolds
    for scaffold in scaffolds:
        assert not (scaffold / FORMER_DIRECTORY).exists(), f"{scaffold} still writes {FORMER_DIRECTORY}/"
        manifest = (scaffold.parent / "manifest.yaml").read_text(encoding="utf-8")
        assert f"{FORMER_DIRECTORY}/" not in manifest
        if (scaffold / DIRECTORY).exists():
            assert f"{DIRECTORY}/workflow.yaml" in manifest
