"""The SDK follows the run directory of the runner it runs under (ADR 0226, R9a).

From 0.30.0 the runner lays `.tranibor/` (and, for the window, a copy under the former `.choregos/`); a
runner before 0.30.0 lays `.choregos/` only, and reads the result there with the former id only. The SDK
reads the new path first, and writes where its runner reads.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from tranibor_agent_sdk.context import Context
from tranibor_agent_sdk.result import Result, write_result


class _Session:
    """What `Context` needs of a session: the workspace, and a guard that allows."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.asked: list[str] = []

    async def guarded(
        self, title: str, kind: str, raw_input: dict[str, Any], action: Callable[[], Awaitable[Any]]
    ) -> Any:
        self.asked.append(raw_input["path"])
        return await action()


def _lay(root: Path, directory: str, task: str) -> None:
    (root / directory / "inputs").mkdir(parents=True)
    (root / directory / "task.md").write_text(task, encoding="utf-8")
    (root / directory / "fields.json").write_text(json.dumps({"from": directory}), encoding="utf-8")
    (root / directory / "inputs" / "bars.csv").write_text("a\n", encoding="utf-8")


def _context(workspace: Path) -> tuple[Context, _Session]:
    session = _Session(workspace)
    return Context(session, "the prompt", 1, env={}), session  # type: ignore[arg-type]


async def test_under_a_runner_of_0_30_the_sdk_reads_and_writes_the_new_directory(tmp_path: Path) -> None:
    _lay(tmp_path, ".tranibor", "# new task\n")
    _lay(tmp_path, ".choregos", "# the copy\n")
    ctx, session = _context(tmp_path)
    assert ctx.task == "# new task\n"
    assert ctx.fields == {"from": ".tranibor"}
    assert ctx.inputs == [tmp_path / ".tranibor/inputs/bars.csv"]
    await ctx.write_output("note.md", "hi")
    assert session.asked == [".tranibor/outputs/note.md"]
    assert (tmp_path / ".tranibor/outputs/note.md").read_text() == "hi"
    path = write_result(tmp_path, Result(summary="done"))
    assert path == tmp_path / ".tranibor/result.json"
    assert json.loads(path.read_text())["schema"] == "tranibor/StageResult/v1"
    assert not (tmp_path / ".choregos/result.json").exists()


async def test_under_a_runner_before_0_30_the_sdk_uses_the_former_directory_and_id(tmp_path: Path) -> None:
    _lay(tmp_path, ".choregos", "# former task\n")
    ctx, session = _context(tmp_path)
    assert ctx.task == "# former task\n"
    assert ctx.fields == {"from": ".choregos"}
    assert ctx.inputs == [tmp_path / ".choregos/inputs/bars.csv"]
    await ctx.write_output("note.md", "hi")
    assert session.asked == [".choregos/outputs/note.md"], "the former runner's guard allows this path"
    path = write_result(tmp_path, Result(summary="done"))
    assert path == tmp_path / ".choregos/result.json"
    assert json.loads(path.read_text())["schema"] == "choregos/StageResult/v1", "the only id it reads"
    assert not (tmp_path / ".tranibor").exists()


def test_without_a_run_directory_the_sdk_writes_the_new_one(tmp_path: Path) -> None:
    ctx, _ = _context(tmp_path)
    assert ctx.task == "the prompt"
    assert ctx.fields == {} and ctx.inputs == []
    assert write_result(tmp_path, Result(summary="done")) == tmp_path / ".tranibor/result.json"
