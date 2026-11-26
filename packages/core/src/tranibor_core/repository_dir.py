# SPDX-License-Identifier: Apache-2.0
"""The directory a repository keeps the platform's files in: `.tranibor/` (ADR 0226 R5).

A repository declares its workflow and its policy there (`.tranibor/workflow.yaml`,
`.tranibor/policy.yaml`), and the scaffold a template opens as a pull request writes them there. The
former directory, `.choregos/`, is read when the new one is absent, for the window of ADR 0226 §1; the
platform never writes it again. A policy keeps agents out of both (`deny_paths`).
"""

from __future__ import annotations

from pathlib import Path

#: The directory, from the root of a repository.
DIRECTORY = ".tranibor"
#: Its former name: read when `DIRECTORY` does not hold the file, never written.
FORMER_DIRECTORY = ".choregos"
#: Both, the current one first: what a policy keeps agents out of.
DIRECTORIES = (DIRECTORY, FORMER_DIRECTORY)


def repository_file(root: Path, name: str) -> Path:
    """`<root>/.tranibor/<name>`, or `<root>/.choregos/<name>` when only the former exists.

    The path returned for a file in neither is the new one: what a caller writes, or names in an error.
    """
    current = root / DIRECTORY / name
    if current.exists():
        return current
    former = root / FORMER_DIRECTORY / name
    return former if former.exists() else current
