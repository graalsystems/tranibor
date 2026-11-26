# SPDX-License-Identifier: Apache-2.0
"""The run directory: where the runner keeps a step's files in its sandbox (ADR 0226, R9a).

It is `.tranibor/` from 0.30.0. Its former name, `.choregos/`, is still what an agent built on an older
agent SDK and a job image built outside the repository read and write, so for the window of ADR 0226 §1
(until 0.32.0):

- what the runner lays down for an agent or a job to READ (`task.md`, `context.md`,
  `allowed_paths.txt`, `fields.json`, `inputs/`) is written under `.tranibor/`, then **copied** under
  `.choregos/` (`mirror`): two real files, no symbolic link. The runner opens nothing through a link
  (`safe_files`), Azure Files shares (the ACA executor) and some volume drivers refuse them, and a
  repository that still tracks `.choregos/workflow.yaml` (ADR 0226 R5) could not have its `.choregos/`
  replaced by one;
- what an agent or a job WRITES (`result.json`, `job.json`, `outputs/`) is read under `.tranibor/`
  first, then under `.choregos/` (`found`, `DIRECTORIES`);
- the runner's own files (the journal, the agent's session, the knowledge bases, the skills, the
  hooks) are written under `.tranibor/` only; a journal or a session a runner of the previous release
  left in a workspace kept across a person's answer is taken over from `.choregos/` (`adopt`, `found`).

Every one of these paths, under either name, is the runner's: it never enters a commit or a pull
request (`excluded`), the scope check does not charge it to the agent, and the guard rails let an agent
write only `result.json` and `outputs/` there.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from tranibor_contracts.wire import FORMER_RUN_DIRECTORY, RUN_DIRECTORIES, RUN_DIRECTORY, run_paths

from . import safe_files

#: `.tranibor`, the directory the runner writes.
DIRECTORY = RUN_DIRECTORY
#: `.choregos`, its former name, mirrored and read for the window.
FORMER_DIRECTORY = FORMER_RUN_DIRECTORY
#: Both, the new one first.
DIRECTORIES = RUN_DIRECTORIES
#: What the runner lays down for an agent or a job to read, copied under the former name for the window.
MIRRORED = ("task.md", "context.md", "allowed_paths.txt", "fields.json", "inputs")


def path(name: str) -> Path:
    """`.tranibor/<name>`, relative to the workspace: where the runner writes."""
    return Path(DIRECTORY) / name


def excluded(name: str) -> list[str]:
    """`[.tranibor/<name>, .choregos/<name>]`: what a commit must never carry, under either name."""
    return list(run_paths(name))


def found(root: Path, name: str) -> Path:
    """`<root>/.tranibor/<name>` when it is a regular file reached through no link, else
    `<root>/.choregos/<name>` when that one is; the new path when neither (what a caller writes, or names
    in an error)."""
    for relative in run_paths(name):
        if safe_files.is_regular(root, relative):
            return root / relative
    return root / path(name)


def adopt(root: Path, name: str) -> None:
    """Copies `.choregos/<name>` to `.tranibor/<name>` when only the former exists: a file of the runner's
    own that a runner of the previous release left in a workspace kept across a person's answer (its
    journal), which this run goes on writing under the new name. Nothing is followed on either side."""
    if _kind(root, path(name)) or not safe_files.is_regular(root, Path(FORMER_DIRECTORY) / name):
        return
    safe_files.write_bytes(root, path(name), safe_files.read_bytes(root, Path(FORMER_DIRECTORY) / name))


def mirror(root: Path, names: Iterable[str] = MIRRORED) -> list[str]:
    """Copies what the runner wrote under `.tranibor/` to the same name under `.choregos/`: each named
    file, and each regular file directly in each named folder. Nothing is followed on either side: a link
    or a second name the agent left in the former directory (a workspace kept across a person's answer)
    raises `LinkRefusedError`, as for the new one. Returns the former paths written."""
    written: list[str] = []
    for name in names:
        source = path(name)
        kind = _kind(root, source)
        if kind == "file":
            files = [source]
        elif kind == "dir":
            regular, _ = safe_files.regular_files(root, source)
            files = [source / entry for entry in regular]
        else:
            continue
        for file in files:
            target = Path(FORMER_DIRECTORY, *file.parts[1:])
            safe_files.write_bytes(root, target, safe_files.read_bytes(root, file))
            written.append(target.as_posix())
    return written


def _kind(root: Path, relative: Path) -> str:
    """`file`, `dir` or `` (absent, a link, anything else), without following a link."""
    try:
        mode = os.lstat(root / relative).st_mode
    except OSError:
        return ""
    if stat.S_ISREG(mode):
        return "file"
    return "dir" if stat.S_ISDIR(mode) else ""
