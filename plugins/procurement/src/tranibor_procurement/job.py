# SPDX-License-Identifier: Apache-2.0
"""Running as a Tranibor job step (ADR 0056): the workspace in, `.tranibor/job.json` out.

The runner gives the command a workspace: the work item's fields in `.tranibor/fields.json`, the files
a step declares in `.tranibor/inputs/`. What the command computes goes back in `.tranibor/job.json` =
`{summary, facts, outputs}`, written whole or not at all. A runner before 0.30.0 lays the run directory
under its former name only: the command then reads and writes there (`run_directory`, ADR 0226 R9a,
until 0.32.0).

The exit code is 0 whenever the contract is kept, even if no file could be read: the facts say what
could not, and the workflow's gate decides. It is non-zero for a broken contract only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

#: The most an inline output may weigh. The runner sets no bound of its own (`tranibor_runner.job`
#: keeps what `job.json` says); the value is what a workflow engine moves comfortably between steps.
MAX_INLINE_BYTES: Final = 2 * 1024 * 1024
#: The run directory, then its former name (ADR 0226, R9a).
RUN_DIRECTORIES: Final = (".tranibor", ".choregos")


class ContractError(Exception):
    """The step cannot honour its contract: the command exits 2."""


def workspace() -> Path:
    """The job's workspace: `TRANIBOR_WORKSPACE`, else the directory the runner started the job in."""
    root = os.environ.get("TRANIBOR_WORKSPACE")
    return Path(root) if root else Path.cwd()


def run_directory(root: Path) -> Path:
    """`<root>/.tranibor`, or the former `<root>/.choregos` when only that one was laid down: the job runs
    under a runner before 0.30.0, which reads its `job.json` there only."""
    new, former = (root / name for name in RUN_DIRECTORIES)
    return former if former.is_dir() and not new.is_dir() else new


def inputs_dir(root: Path) -> Path:
    path = run_directory(root) / "inputs"
    if not path.is_dir():
        raise ContractError(
            f"no inputs directory at {path}: the step declares no input, or this is not a job"
        )
    return path


def read_fields(root: Path) -> dict[str, Any]:
    path = run_directory(root) / "fields.json"
    if not path.is_file():
        return {}
    try:
        fields = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(fields, dict):
        raise ContractError(f"{path} is not a JSON object")
    return fields


def publish(root: Path, *, summary: str, facts: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """Writes `.tranibor/job.json`; refuses an inline output past the bound, and writes nothing then."""
    doc = {"summary": summary, "facts": facts, "outputs": outputs}
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    for name, value in outputs.items():
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > MAX_INLINE_BYTES:
            raise ContractError(
                f"the output `{name}` weighs {size} bytes, the most an inline output may weigh is "
                f"{MAX_INLINE_BYTES}: lower --max-chars-per-file or hand fewer files"
            )
    target = run_directory(root) / "job.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".json.tmp")
    scratch.write_text(body, encoding="utf-8")
    scratch.replace(target)
    return doc
