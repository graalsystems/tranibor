"""The run directory takes the name `tranibor`, and the former `.choregos/` still works (ADR 0226, R9a).

The runner writes `.tranibor/`. For the window, what an agent or a job reads is copied under
`.choregos/` as real files, what they write is read under `.tranibor/` first and then `.choregos/`, and
every path of either directory is the runner's: never in a commit, never charged to the agent's scope.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tranibor_contracts import StageInput, StageStatus
from tranibor_contracts.delivery import repository_of
from tranibor_runner import run_dir, safe_files
from tranibor_runner.exits import Exit
from tranibor_runner.outputs import collect
from tranibor_runner.runner import Runner, _saved_session
from tranibor_runner.scope import config_files_touched
from tranibor_runner.workspace import RUNNER_FILES, Workspace

from .conftest import FakeInternalClient, agent_script, valid_result


def _write(root: Path, relative: str, data: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


# ─── what the runner lays down for an agent or a job to read ─────────────────────────────────────────


def test_what_an_agent_reads_is_laid_under_both_names_as_real_files(tmp_path: Path) -> None:
    for name, text in (("task.md", "# task\n"), ("fields.json", "{}\n"), ("inputs/bars.csv", "a\n")):
        _write(tmp_path, f".tranibor/{name}", text)
    written = run_dir.mirror(tmp_path)
    assert sorted(written) == [".choregos/fields.json", ".choregos/inputs/bars.csv", ".choregos/task.md"]
    for name in ("task.md", "fields.json", "inputs/bars.csv"):
        former = tmp_path / ".choregos" / name
        assert former.read_bytes() == (tmp_path / ".tranibor" / name).read_bytes()
        assert not former.is_symlink() and former.stat().st_nlink == 1, "a copy, never a link"
    assert not (tmp_path / ".choregos").is_symlink()


def test_the_copy_follows_no_link_an_agent_left_under_the_former_name(tmp_path: Path) -> None:
    """A workspace kept across a person's answer: the agent may have made `.choregos` a link to a file
    of the runner's. The copy refuses, and writes nothing through it."""
    _write(tmp_path, ".tranibor/task.md", "# task\n")
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, tmp_path / ".choregos")
    with pytest.raises(safe_files.LinkRefusedError):
        run_dir.mirror(tmp_path)
    assert list(target.iterdir()) == []


# ─── what an agent or a job writes ───────────────────────────────────────────────────────────────────


def test_a_result_is_found_under_the_new_name_first_then_the_former(tmp_path: Path) -> None:
    assert run_dir.found(tmp_path, "result.json") == tmp_path / ".tranibor/result.json", "none yet"
    _write(tmp_path, ".choregos/result.json", "{}")
    assert run_dir.found(tmp_path, "result.json") == tmp_path / ".choregos/result.json"
    _write(tmp_path, ".tranibor/result.json", "{}")
    assert run_dir.found(tmp_path, "result.json") == tmp_path / ".tranibor/result.json"
    assert Workspace(tmp_path).result_path() == tmp_path / ".tranibor/result.json"


def test_a_former_result_behind_a_link_is_not_followed(tmp_path: Path) -> None:
    (tmp_path / ".choregos").mkdir()
    os.symlink("/proc/self/environ", tmp_path / ".choregos" / "result.json")
    assert run_dir.found(tmp_path, "result.json") == tmp_path / ".tranibor/result.json"


def test_deliverables_are_collected_from_both_folders_the_new_one_first(
    tmp_path: Path, stage_input: StageInput
) -> None:
    stage_input.transition.outputs = ["deck"]
    stage_input.transition.output_types = {"deck": "pptx"}
    _write(tmp_path, ".choregos/outputs/deck.pptx", b"former deck")
    _write(tmp_path, ".choregos/outputs/chart.png", b"former chart")
    kept, refused = collect(tmp_path, stage_input)
    assert not refused
    assert {(name, path.relative_to(tmp_path).as_posix()) for name, _, path in kept} == {
        ("deck", ".choregos/outputs/deck.pptx"),
        ("chart.png", ".choregos/outputs/chart.png"),
    }
    _write(tmp_path, ".tranibor/outputs/deck.pptx", b"new deck")
    kept, refused = collect(tmp_path, stage_input)
    assert not refused
    assert {(name, path.relative_to(tmp_path).as_posix()) for name, _, path in kept} == {
        ("deck", ".tranibor/outputs/deck.pptx"),
        ("chart.png", ".choregos/outputs/chart.png"),
    }, "a name in both folders is the new folder's, once"


# ─── the runner's own files ──────────────────────────────────────────────────────────────────────────


def test_a_journal_the_previous_release_left_is_taken_over(tmp_path: Path) -> None:
    _write(tmp_path, ".choregos/transcript.jsonl", "{}\n{}\n")
    run_dir.adopt(tmp_path, "transcript.jsonl")
    assert (tmp_path / ".tranibor/transcript.jsonl").read_text() == "{}\n{}\n"
    _write(tmp_path, ".choregos/transcript.jsonl", "{}\n{}\n{}\n")
    run_dir.adopt(tmp_path, "transcript.jsonl")
    assert (tmp_path / ".tranibor/transcript.jsonl").read_text() == "{}\n{}\n", "the new one is kept"


def test_a_session_the_previous_release_saved_is_reloaded(tmp_path: Path) -> None:
    _write(tmp_path, ".choregos/acp-session.json", '{"backend": "fake", "session_id": "s-old"}')
    assert _saved_session(tmp_path) == {"backend": "fake", "session_id": "s-old"}
    _write(tmp_path, ".tranibor/acp-session.json", '{"backend": "fake", "session_id": "s-new"}')
    assert _saved_session(tmp_path) == {"backend": "fake", "session_id": "s-new"}


def test_both_directories_are_the_runner_s() -> None:
    for name in ("task.md", "result.json", "fields.json", "job.json", "transcript.jsonl"):
        assert {f".tranibor/{name}", f".choregos/{name}"} <= set(RUNNER_FILES)
    assert config_files_touched([".choregos/task.md", ".tranibor/allowed_paths.txt", "src/a.py"], Path()) == [
        ".choregos/task.md",
        ".tranibor/allowed_paths.txt",
    ]
    assert config_files_touched([".choregos/result.json", ".tranibor/result.json"], Path()) == []


# ─── end to end: an agent built on an older SDK ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_that_knows_only_the_former_directory_works_and_leaks_nothing(
    stage_input: StageInput, runner_settings: Any, toy_repo: Path
) -> None:
    """It reads `.choregos/task.md`, writes its deck in `.choregos/outputs/` and its result in
    `.choregos/result.json`, beside a change to the code: the step is done, the deck is stored, and the
    branch pushed for the pull request carries the code change only, under neither directory."""
    stage_input.transition.outputs = ["deck"]
    stage_input.transition.output_types = {"deck": "pptx"}
    deck = b"PK\x03\x04 a deck"
    agent_script(
        {
            "turns": [
                {
                    "reads": [".choregos/task.md", ".choregos/context.md", ".choregos/allowed_paths.txt"],
                    "writes": [
                        {"path": ".choregos/outputs/deck.pptx", "b64": base64.b64encode(deck).decode()},
                        {"path": "src/orders.py", "content": "TOTAL = 2\n"},
                    ],
                    "result_dir": ".choregos",
                    "result": valid_result("deck ready"),
                }
            ]
        },
        stage_input,
        runner_settings.workspace,
    )
    client = FakeInternalClient(stage_input)
    outcome = await Runner(runner_settings, client).execute(stage_input, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.OK, outcome.detail
    result = client.results[-1]
    assert result.status is StageStatus.DONE, result.summary
    assert result.summary == "deck ready", "the result was read under `.choregos/`"
    assert all(result.outputs.model_dump()["seen"].values()), "the agent found its task under the former name"
    assert client.outputs["deck"][1] == deck  # type: ignore[attr-defined]
    repository = repository_of(stage_input)
    assert repository is not None
    pushed = subprocess.run(
        ["git", "diff", "--name-only", "main", repository.work_branch],
        cwd=toy_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert pushed == ["src/orders.py"], f"the run directory leaked into the pull request: {pushed}"
