# SPDX-License-Identifier: Apache-2.0
"""The activities the core registers where no installed extension does (ADR 0181, round D).

The interpreter calls `collect_run_artifacts` by name after every agent step, the name every recorded
history carries: it files the run's diff in its result, for the console. The diff is read from the
project's source control, which is the `software-delivery` plugin's, and so is the activity
(`tranibor_software_delivery.source_control`). Where that extension is not installed, a step must not
fail on an activity no worker registers: the core registers this stand-in under the same name, which
files nothing. Where the extension registers its own, the stand-in is left out
(`extension_parts.all_activities`), so one implementation answers each name.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

#: The activity the interpreter calls after every agent step, by the name histories carry.
RUN_ARTIFACTS = "collect_run_artifacts"


@activity.defn(name=RUN_ARTIFACTS)
async def collect_run_artifacts(payload: dict[str, Any]) -> dict[str, Any]:
    """Nothing to file: this deployment reads no diff (no installed extension serves a source control)."""
    return {"files": 0}


#: What `all_activities` adds when no extension provides an activity of the same name.
STAND_INS = [collect_run_artifacts]
