"""The workflow a repository declares is `.tranibor/workflow.yaml`; `.choregos/` is read when it is absent.

ADR 0226 R5: `tranibor workflow validate|show|push` without a path read the new directory, and a repository
that still has only the former one keeps working for the window.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tranibor_core.repository_dir import DIRECTORIES, repository_file
from typer.testing import CliRunner

WORKFLOW = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: NAME, version: 1}
actors:
  dev: {type: human, group: devs}
states:
  todo: {display: To do, kind: wait}
  done: {display: Done, terminal: true}
transitions:
  - {id: t-do, from: todo, to: done, by: dev}
"""


def _write(root: Path, directory: str, name: str) -> Path:
    path = root / directory / "workflow.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW.replace("NAME", name), encoding="utf-8")
    return path


def _run(*args: str) -> tuple[int, str]:
    from tranibor_cli.main import app

    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.output


def test_the_directory_is_named_tranibor_and_the_former_one_is_still_known() -> None:
    assert DIRECTORIES == (".tranibor", ".choregos")


def test_the_new_directory_wins_and_the_former_one_is_read_when_it_is_absent(tmp_path: Path) -> None:
    assert repository_file(tmp_path, "workflow.yaml") == tmp_path / ".tranibor" / "workflow.yaml"
    former = _write(tmp_path, ".choregos", "former")
    assert repository_file(tmp_path, "workflow.yaml") == former
    current = _write(tmp_path, ".tranibor", "current")
    assert repository_file(tmp_path, "workflow.yaml") == current


@pytest.mark.parametrize(
    ("directories", "shown"), [((".tranibor",), ".tranibor"), ((".choregos",), ".choregos")]
)
def test_validate_without_a_path_reads_the_repository_s_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directories: tuple[str, ...], shown: str
) -> None:
    monkeypatch.chdir(tmp_path)
    for directory in directories:
        _write(tmp_path, directory, "delivery")
    code, output = _run("workflow", "validate")
    assert code == 0, output
    assert f"{shown}/workflow.yaml is valid" in output


def test_show_without_a_path_prefers_the_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".choregos", "former")
    (tmp_path / ".tranibor").mkdir()
    (tmp_path / ".tranibor" / "workflow.yaml").write_text(
        WORKFLOW.replace("NAME", "current").replace("To do", "Fresh"), encoding="utf-8"
    )
    code, output = _run("workflow", "show")
    assert code == 0, output
    assert "Fresh" in output and "To do" not in output


def test_without_either_directory_the_error_names_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_cli.main import DEFAULT_WORKFLOW, app, workflow_file

    monkeypatch.chdir(tmp_path)
    assert workflow_file(DEFAULT_WORKFLOW) == Path(".tranibor") / "workflow.yaml"
    result = CliRunner().invoke(app, ["workflow", "validate"])
    assert result.exit_code != 0
    assert ".tranibor/workflow.yaml" in str(result.exception)
