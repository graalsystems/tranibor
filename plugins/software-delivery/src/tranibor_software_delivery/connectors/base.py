# SPDX-License-Identifier: Apache-2.0
"""What a source control, a continuous integration and a continuous deployment connector do (ADR 0181,
round D): the protocols the `scm`, `ci` and `cd` slots' adapters implement. They left
`tranibor_adapters.base` with the types."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from tranibor_contracts import InboundEvent
from tranibor_core.domain import (
    Change,
    CiStatus,
    DiffSummary,
    Health,
    PromotionRef,
    PrRef,
    PrState,
    RolloutState,
    Window,
)

#: What an SCM can do beyond answering the protocol. An adapter declares the subset it really
#: implements (same discipline as `Executor.capabilities`): a capability announced and absent is
#: worse than absent, the caller relies on it.
#:
#: - `merge_at_commit`: `enqueue_merge` names the commit the pull request's head must be at, and
#:   the forge refuses the merge when the head is elsewhere (ADR 0171). The orchestrator never asks
#:   an adapter without it to merge: it refuses, rather than merge a head no gate judged.
SCM_CAPABILITIES: frozenset[str] = frozenset({"merge_at_commit"})


def merge_at_commit_refusal(scm: Any) -> str | None:
    """Why `scm` cannot merge the commit the gates judged (ADR 0171), or None when it can.

    The gates rule on one commit; a merge that names none merges whatever the branch holds by then.
    An adapter that cannot name it is refused, never called (ADR 0101).
    """
    if "merge_at_commit" in getattr(scm, "capabilities", frozenset()):
        return None
    return (
        f"the `{type(scm).__name__}` SCM declares no `merge_at_commit`: it cannot hold a merge to the "
        "commit the gates judged, so the platform does not merge through it"
    )


@runtime_checkable
class ScmAdapter(Protocol):
    """The SCM: branches, PRs, checks, merge queue. The tokens are minted per run."""

    #: What THIS adapter can do, taken from `SCM_CAPABILITIES`.
    capabilities: frozenset[str]

    async def mint_token(self, repo: str, ttl_s: int, scopes: list[str]) -> str: ...
    async def ensure_branch(self, repo: str, name: str, base: str) -> None: ...
    #: Whether the branch exists on the repository (S22-46): no pull request is asked for without one.
    async def branch_exists(self, repo: str, name: str) -> bool: ...
    #: The commit the branch points at now, `None` when the branch does not exist (ADR 0171): the
    #: gates judge that commit, not whatever the name designates a moment later.
    async def head_of(self, repo: str, branch: str) -> str | None: ...
    async def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str: ...
    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> PrRef: ...
    async def update_pr(self, ref: PrRef, body: str | None, draft: bool | None) -> None: ...
    async def comment_pr(self, ref: PrRef, body: str) -> str: ...
    async def get_pr(self, ref: PrRef) -> PrState: ...
    async def request_review(self, ref: PrRef, reviewers: list[str]) -> None: ...
    #: Merge the pull request at `sha`, the commit the gates judged (ADR 0171). Its head elsewhere is a
    #: `HeadMovedError` and nothing is merged; already merged at `sha`, it is left as it is.
    async def enqueue_merge(self, ref: PrRef, sha: str) -> None: ...
    async def compare(self, repo: str, base: str, head: str) -> DiffSummary: ...
    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[InboundEvent]: ...


@runtime_checkable
class CiAdapter(Protocol):
    async def status_for(self, repo: str, sha: str) -> CiStatus: ...
    async def logs(self, run_ref: str, tail: int = 500) -> str: ...
    async def trigger(self, repo: str, ref: str, pipeline: str) -> str: ...
    def parse_event(self, headers: Mapping[str, str], body: bytes) -> list[InboundEvent]: ...


@runtime_checkable
class CdAdapter(Protocol):
    async def current_revision(self, app: str) -> str: ...
    async def promote(self, env: str, changes: list[Change], release: str) -> PromotionRef: ...
    async def health(self, app: str) -> Health: ...
    async def rollout_status(self, app: str) -> RolloutState: ...
    async def abort_rollout(self, app: str) -> None: ...
    async def set_sync_window(self, app: str, windows: list[Window]) -> None: ...
