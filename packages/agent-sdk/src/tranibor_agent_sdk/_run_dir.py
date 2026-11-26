# SPDX-License-Identifier: Apache-2.0
"""Where the runner keeps the step's files in the workspace: `.tranibor/` (ADR 0226, R9a).

A runner before 0.30.0 lays them under the former `.choregos/` and reads the result there only, with
the former `schema` id only. The SDK follows the runner it runs under: the new directory when the runner
laid it (or when neither exists), the former one when only that one exists, until 0.32.0.
"""

from __future__ import annotations

from pathlib import Path

from tranibor_contracts.wire import FORMER_RUN_DIRECTORY, RUN_DIRECTORY, schema_ids


def run_directory(workspace: Path) -> Path:
    """`<workspace>/.tranibor`, or `<workspace>/.choregos` when only the former was laid down."""
    new, former = workspace / RUN_DIRECTORY, workspace / FORMER_RUN_DIRECTORY
    return former if former.is_dir() and not new.is_dir() else new


def is_former(directory: Path) -> bool:
    """Whether `directory` is the former run directory: its runner reads only the former `schema`."""
    return directory.name == FORMER_RUN_DIRECTORY


def readable(workspace: Path, name: str) -> Path:
    """`.tranibor/<name>` when it exists, else `.choregos/<name>` when that one does, else the new path."""
    for directory in (RUN_DIRECTORY, FORMER_RUN_DIRECTORY):
        if (workspace / directory / name).exists():
            return workspace / directory / name
    return workspace / RUN_DIRECTORY / name


#: The result's `schema`: the new id, and the former one a runner before 0.30.0 requires.
RESULT_SCHEMA, FORMER_RESULT_SCHEMA = schema_ids("StageResult")
