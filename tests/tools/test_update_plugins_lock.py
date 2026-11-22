"""`plugins/offline/` and its `plugins.lock` (S43-09): the archives are what the lock says.

`make plugins-check` runs the same check; a refresh (`make plugins-lock`) needs the network.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from tranibor_core.publishing import PUBLISHING

from tools.plugins import update_lock
from tools.plugins.update_lock import problems, read

ROOT = Path(__file__).resolve().parents[2]
OFFLINE = ROOT / "plugins" / "offline"
#: Any index but the publishing one: the frozen source's, whose registry answers 403 (#2140).
ANOTHER_INDEX = "ghcr.io/example/former-agents/index:latest"


def test_the_archives_are_what_the_lock_says() -> None:
    assert problems(OFFLINE) == []
    index, locked = read(OFFLINE)
    assert set(locked) == {
        "software-delivery",
        "netops",
        "authoring",
        "mcp-clients",
        "requests",
        "investment-research",
        "procurement",
        "procurement-operations",
    }
    # The lock records the index a refresh reads: the publishing identity's (ADR 0156), never the frozen
    # source's, whose registry refuses it (#2140).
    assert index == PUBLISHING.plugin_index.removeprefix("oci://")
    current = index.rsplit("/", 1)[0]
    for entry in locked.values():
        assert entry.source == f"{current}/{entry.name}:{entry.version}", entry
        assert entry.signature == f"keyless, {PUBLISHING.plugin_signing_identity}", entry


def test_a_changed_archive_or_an_unlisted_one_is_caught(tmp_path: Path) -> None:
    shutil.copytree(OFFLINE, tmp_path / "offline")
    copy = tmp_path / "offline"
    shutil.copy(copy / "netops-1.2.2.tar.gz", copy / "extra-1.0.0.tar.gz")
    with tarfile.open(OFFLINE / "authoring-1.1.2.tar.gz") as source:
        source.extractall(tmp_path / "authoring", filter="data")
    agent = tmp_path / "authoring" / "agents" / "agent-drafter.yaml"
    agent.write_text(agent.read_text(encoding="utf-8") + "\n# one more line\n", encoding="utf-8")
    with tarfile.open(copy / "authoring-1.1.2.tar.gz", "w:gz") as target:
        for path in sorted((tmp_path / "authoring").rglob("*")):
            target.add(path, path.relative_to(tmp_path / "authoring").as_posix(), recursive=False)
    found = problems(copy)
    assert any("extra-1.0.0.tar.gz: an archive the lock does not name" in p for p in found), found
    assert any(p.startswith("authoring 1.1.2 is sha256:") for p in found), found


def test_a_refresh_reads_the_publishing_index_not_the_lock_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2140: the lock named the frozen source's index, whose registry answers 403."""
    shutil.copytree(OFFLINE, tmp_path / "offline")
    copy = tmp_path / "offline"
    lock = copy / "plugins.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(f"index: {update_lock.INDEX}", f"index: {ANOTHER_INDEX}"),
        encoding="utf-8",
    )
    asked: list[tuple[str, dict[str, str | None]]] = []

    async def refreshed(
        directory: Path, index_ref: str, wanted: dict[str, str | None], verify: bool, cosign: str
    ) -> dict[str, update_lock.Locked]:
        asked.append((index_ref, wanted))
        return {}

    monkeypatch.setattr(update_lock, "_refresh", refreshed)
    assert update_lock.main(["--directory", str(copy), "--no-verify", "--plugin", "netops@1.2.2"]) == 0
    assert asked == [(update_lock.INDEX, {"netops": "1.2.2"})], "only the plugin named moves"
    assert update_lock.read(copy)[0] == update_lock.INDEX, "the lock is rewritten to the index it read"
    assert update_lock.main(["--directory", str(copy), "--no-verify"]) == 0
    assert asked[-1] == (update_lock.INDEX, {n: e.version for n, e in update_lock.read(copy)[1].items()})


def _cosign(tmp_path: Path, version: str) -> str:
    binary = tmp_path / f"cosign-{version}"
    binary.write_text(f'#!/bin/sh\necho \'{{"gitVersion": "{version}"}}\'\n', encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


def test_verifying_needs_cosign_v3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#2140: cosign v2 finds no signature on the source's v3 bundles; the tool says so before pulling."""
    old = _cosign(tmp_path, "v2.6.1")
    assert update_lock.cosign_problem(old) is not None
    assert update_lock.cosign_problem(_cosign(tmp_path, "v3.1.3")) is None
    assert update_lock.cosign_problem(str(tmp_path / "absent")) is not None
    assert update_lock.main(["--directory", str(OFFLINE), "--cosign", old]) == 1
    assert "v2.6.1" in capsys.readouterr().err
