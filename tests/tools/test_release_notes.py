"""`tools/release_notes.py` on small repositories: where a release's notes start, and their cap.

A linear history starts from the nearest earlier release; a rewritten one (v0.29.4: no release tag
was an ancestor any more) from the copy of the previous release's commit; a first release from its
own history. The job that runs it is read by `tests/ci/test_release_notes_job.py`.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from tools import release_notes

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a",
    "GIT_AUTHOR_EMAIL": "a@example.org",
    "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@example.org",
}


def _git(repo: pathlib.Path, *args: str, date: str | None = None) -> str:
    env = dict(_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    done = subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _commit(repo: pathlib.Path, subject: str, day: int, *, rewritten: bool = False) -> str:
    # A rewrite keeps the author date and the subject; the tree (here, the file) changes.
    (repo / "file").write_text(f"{subject} {rewritten}\n", encoding="utf-8")
    _git(repo, "add", "file")
    _git(repo, "commit", "-q", "-m", subject, date=f"2026-11-{day:02d}T10:00:00+00:00")
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: pathlib.Path) -> pathlib.Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_a_linear_history_starts_from_the_nearest_earlier_release(tmp_path: pathlib.Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "feat: one", 1)
    _git(repo, "tag", "v0.1.0")
    first = _commit(repo, "chore(release): 0.1.1", 2)
    _git(repo, "tag", "v0.1.1")
    _git(repo, "tag", "nightly")
    _commit(repo, "feat: two", 3)
    _git(repo, "tag", "v0.2.0")
    found = release_notes.base(repo, "v0.2.0")
    assert found == release_notes.Base(range=f"{first}..v0.2.0", previous_tag="v0.1.1", kind="ancestor")


def test_a_rewritten_history_starts_from_the_copy_of_the_previous_release(
    tmp_path: pathlib.Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "feat: one", 1)
    _commit(repo, "chore(release): 0.1.0", 2)
    _git(repo, "tag", "v0.1.0")
    # main rewritten: an orphan branch whose commits carry the same author dates and subjects.
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "rm", "-rfq", "--cached", ".")
    _commit(repo, "feat: one", 1, rewritten=True)
    copy = _commit(repo, "chore(release): 0.1.0", 2, rewritten=True)
    _commit(repo, "feat: two", 3, rewritten=True)
    _git(repo, "tag", "v0.1.1")
    assert _git(repo, "rev-parse", "v0.1.0") != copy
    found = release_notes.base(repo, "v0.1.1")
    assert found == release_notes.Base(range=f"{copy}..v0.1.1", previous_tag="", kind="rewritten")
    count = _git(repo, "rev-list", "--count", found.range)
    assert count == "1"


def test_a_first_release_takes_its_own_history(tmp_path: pathlib.Path) -> None:
    repo = _repo(tmp_path)
    head = _commit(repo, "feat: one", 1)
    _git(repo, "tag", "v0.1.0")
    _git(repo, "tag", "v0.2.0")
    assert release_notes.base(repo, "v0.1.0") == release_notes.Base(range=head, previous_tag="", kind="none")


def test_the_cap_cuts_at_a_line_and_says_where_the_full_list_is(tmp_path: pathlib.Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("".join(f"- change number {n}\n" for n in range(5000)), encoding="utf-8")
    assert release_notes.cap(notes, 2000, "https://example.org/commits/v1.0.0")
    text = notes.read_text(encoding="utf-8")
    assert len(text.encode()) <= 2000
    assert text.endswith("the full list is https://example.org/commits/v1.0.0_\n")
    body = text.split("\n\n_The list is cut")[0]
    assert all(line.startswith("- change number ") for line in body.splitlines())
    assert body.splitlines()[-1] == f"- change number {body.count(chr(10))}"
    short = tmp_path / "short.md"
    short.write_text("- one\n", encoding="utf-8")
    assert not release_notes.cap(short, 2000, "https://example.org")
    assert short.read_text(encoding="utf-8") == "- one\n"


def test_the_command_prints_github_output_lines(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "feat: one", 1)
    _git(repo, "tag", "v0.1.0")
    _commit(repo, "feat: two", 2)
    _git(repo, "tag", "v0.1.1")
    monkeypatch.chdir(repo)
    assert release_notes.main(["base", "v0.1.1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == ["previous_tag=v0.1.0", "kind=ancestor"]
    assert lines[0].startswith("range=") and lines[0].endswith("..v0.1.1")
