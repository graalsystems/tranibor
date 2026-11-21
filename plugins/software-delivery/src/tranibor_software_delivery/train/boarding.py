# SPDX-License-Identifier: Apache-2.0
"""Boarding the release train (ADR 0181, round D): the activity `signal_train` the interpreter calls by name
for a `via: release_train` transition (`tranibor_core.carriers.BOARD_ACTIVITY`), and `signal_release_train`,
the signal-with-start of an environment's train. They were the orchestrator's; their names, input and the
signal they send are the ones every recorded history carries.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity
from tranibor_core import utcnow
from tranibor_orchestrator.activities.base import db, load_work_item, project_bundle
from tranibor_orchestrator.train_client import signal_workflow


async def signal_release_train(project_slug: str, env: str, name: str, payload: dict[str, Any]) -> None:
    """Signal the train of `env`, starting it with that signal if it does not run (`train-<slug>-<env>`)."""
    await signal_workflow(
        f"train-{project_slug}-{env}",
        name,
        {**payload, "start_payload": {"project_slug": project_slug, "env": env}},
        start="ReleaseTrain",
    )


@activity.defn(name="signal_train")
async def signal_train(payload: dict[str, Any]) -> dict[str, Any]:
    """Announce to the environment's train that a work item is ready to board, with its labels and
    the approval its workflow requires of the departure (ADR 0041), absent from earlier boardings."""
    async with db() as session:
        bundle = await project_bundle(session, payload["project_id"])
        item = await load_work_item(session, payload["work_item_id"])
        await signal_release_train(
            bundle.slug,
            payload["env"],
            "merged",
            {
                "work_item_key": item.tracker_key,
                "title": item.title,
                "merged_at": utcnow().isoformat(),
                "sha": (item.documents or {}).get("merge_sha", ""),
                # Declared by the agent in `artifacts.reports["infra_pr"]`: the train will apply it
                # through Atlantis during the departure, after approval.
                "infra_pr_url": (item.documents or {}).get("infra_pr_url"),
                "risk": item.risk,
                # The work item's labels, kept since its birth: `hotfix` opens the express lane
                # (#279). A work item born before this change kept none.
                "labels": list((item.documents or {}).get("labels") or []),
                "pr_url": item.pr_url,
                **({"approval": payload["approval"]} if payload.get("approval") else {}),
            },
        )
        return {"signalled": True}
