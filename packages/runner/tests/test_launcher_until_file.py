# SPDX-License-Identifier: Apache-2.0
"""S87-08: the agent's container ends with the runner where the platform waits for every container.

A Container Apps execution ends only once every container of its replica has ended (measured on a real
subscription, S87-07): a launcher that serves forever held the execution open until its timeout, then
reported it failed. The runner writes its exit code to the file the executor names, and the launcher,
watching that file, ends.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from tranibor_runner import cli
from tranibor_runner.launcher import serve


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_the_launcher_ends_once_the_runner_s_done_file_exists(tmp_path: Path) -> None:
    done = tmp_path / ".runner-done"
    launcher = asyncio.create_task(serve(port=_free_port(), until=str(done), every=0.05))
    await asyncio.sleep(0.3)
    assert not launcher.done(), "the launcher serves until the file exists"
    done.write_text("0", encoding="utf-8")
    await asyncio.wait_for(launcher, timeout=5)


async def test_without_the_option_the_launcher_keeps_serving(tmp_path: Path) -> None:
    (tmp_path / ".runner-done").write_text("0", encoding="utf-8")
    launcher = asyncio.create_task(serve(port=_free_port()))
    await asyncio.sleep(0.3)
    assert not launcher.done(), "a warm sandbox's launcher must not end on its own"
    launcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launcher


def test_the_runner_writes_its_exit_code_where_the_executor_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = tmp_path / ".runner-done"
    monkeypatch.setenv("TRANIBOR_RUNNER_DONE_FILE", str(done))
    cli._tell_the_agents_container(40)
    assert done.read_text(encoding="utf-8") == "40"


def test_without_the_setting_the_runner_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRANIBOR_RUNNER_DONE_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    cli._tell_the_agents_container(0)
    assert list(tmp_path.iterdir()) == []
