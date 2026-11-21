# SPDX-License-Identifier: Apache-2.0
"""How a build reads as a check (`CheckRun`): the Azure DevOps builds and the GitLab jobs, read by both a
pipeline's connector and the source control's pull request checks.

The `ci` connectors are the `software-delivery` plugin's since round D of ADR 0181; the source control
connectors that read the same builds (`scm/azure_repos.py`, `scm/gitlab.py`) follow them in the next step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from tranibor_core.domain import CheckRun

State = Literal["pending", "running", "success", "failure", "error", "unknown"]
Progress = Literal["queued", "in_progress", "completed"]
Conclusion = Literal["success", "failure", "neutral", "cancelled", "timed_out", "skipped"]

_STATUS_PROGRESS: dict[str, Progress] = {
    "notStarted": "queued",
    "postponed": "queued",
    "none": "queued",
    "inProgress": "in_progress",
    "cancelling": "in_progress",
}
_RESULT: dict[str, Conclusion] = {
    "succeeded": "success",
    "failed": "failure",
    "canceled": "cancelled",
    "skipped": "skipped",
    "abandoned": "cancelled",
}


def check_of(
    name: str, status: str, result: str | None, *, partial: Conclusion = "failure", url: str | None = None
) -> CheckRun:
    """One build, job or stage as a check: where it is, and how it ended once it has."""
    if status != "completed":
        return CheckRun(name=name, status=_STATUS_PROGRESS.get(status, "in_progress"), url=url)
    if result in {"partiallySucceeded", "succeededWithIssues"}:
        conclusion: Conclusion = partial
    else:
        # A completed build whose result is unreadable is not a pass.
        conclusion = _RESULT.get(str(result), "failure")
    return CheckRun(name=name, status="completed", conclusion=conclusion, url=url)


def state_of(checks: list[CheckRun]) -> State:
    """The state of the latest build of each pipeline together: a failure outranks what still runs."""
    if not checks:
        return "unknown"
    if any(c.status == "completed" and c.conclusion in {"failure", "timed_out"} for c in checks):
        return "failure"
    if any(c.status == "in_progress" for c in checks):
        return "running"
    if any(c.status == "queued" for c in checks):
        return "pending"
    if any(c.conclusion == "cancelled" for c in checks):
        return "error"
    return "success"


def source_commit_of(build: Mapping[str, Any]) -> str:
    """The commit a build was asked to build: for a build of a pull request's merge, the pull request's own
    source commit, since `sourceVersion` is then the merge commit Azure made."""
    info = build.get("triggerInfo") or {}
    pr = info.get("pr.sourceCommitId") or info.get("pr.sourceSha")
    return str(pr or build.get("sourceVersion") or "")


def build_name_of(build: Mapping[str, Any]) -> str:
    """`<pipeline> #<build number>`: what a check of a build is called."""
    pipeline = (build.get("definition") or {}).get("name", "pipeline")
    return f"{pipeline} #{build.get('buildNumber') or build.get('id')}"


def web_url_of(build: Mapping[str, Any]) -> str | None:
    web = ((build.get("_links") or {}).get("web") or {}).get("href")
    return str(web) if web else None


def latest_per_pipeline(builds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first in, the first of each pipeline out."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for build in builds:
        key = (build.get("definition") or {}).get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(build)
    return out


#: A GitLab job's status, as a check: where it is and how it ended.
GITLAB_JOB_RUN: dict[str, tuple[Progress, Conclusion | None]] = {
    "success": ("completed", "success"),
    "failed": ("completed", "failure"),
    "running": ("in_progress", None),
    "pending": ("queued", None),
    "created": ("queued", None),
    "waiting_for_resource": ("queued", None),
    "preparing": ("queued", None),
    "scheduled": ("queued", None),
    "canceled": ("completed", "cancelled"),
    "skipped": ("completed", "skipped"),
    "manual": ("completed", "neutral"),
}
