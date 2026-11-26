# SPDX-License-Identifier: Apache-2.0
"""The workspace an agent declares it needs (ADR 0119): `none`, `read` or `write`.

Shared by the API, which validates and shows the declaration, and the orchestrator, which turns it
into the run's `StageInput`: no `repo` for `none`, a write scope that matches no repository file for
`read`, the work item's scope for `write`.
"""

from __future__ import annotations

from typing import Literal

from tranibor_contracts.wire import FORMER_RUN_DIRECTORY, RUN_DIRECTORY

AgentWorkspace = Literal["none", "read", "write"]

#: The write scope of a `read` run. An EMPTY scope would mean "no scope declared: write allowed" to
#: the runner's guards (`GuardRails.check_write`, `check_scope`), so the scope is a pattern that
#: matches no repository file: the step's deliverables (ADR 0049), which are never committed.
READ_ONLY_WRITE_PATHS: tuple[str, ...] = (f"{RUN_DIRECTORY}/outputs/**",)
#: The same scope under the run directory's former name (ADR 0226, R9a): what a run started before
#: 0.30.0 recorded, still recognised as read-only until 0.32.0.
FORMER_READ_ONLY_WRITE_PATHS: tuple[str, ...] = (f"{FORMER_RUN_DIRECTORY}/outputs/**",)


def write_paths_for(workspace: str, allowed: list[str]) -> list[str]:
    """The paths a run of an agent with this workspace may write: `allowed` (the work item's scope,
    `**` when it has none) unless the agent only reads the repository."""
    if workspace == "read":
        return list(READ_ONLY_WRITE_PATHS)
    return list(allowed) or ["**"]


def is_read_only_scope(paths: list[str] | None) -> bool:
    """Whether a run's recorded scope is the one a `read` workspace gets: such a run never widens it. A
    run recorded before 0.30.0 holds the former form (ADR 0226, R9a)."""
    return list(paths or []) in (list(READ_ONLY_WRITE_PATHS), list(FORMER_READ_ONLY_WRITE_PATHS))
