# SPDX-License-Identifier: Apache-2.0
"""A merged work item boards the release train its workflow takes (ADR 0041, ADR 0181 round D).

A forge's webhook (`githubWebhook`, `gitlabWebhook`, `azureDevopsWebhook`) delivers `scm.pr.merged` to its
work item; the core's `dispatch` then runs the hook the extension declared for that type
(`extensions.declare_inbound_hook`), and records its outcome, `boarded`. It was the core's
`routers/webhooks._board_train` until round D; the signal it sends the train is unchanged.
"""

from __future__ import annotations

from typing import Any

from tranibor_api.services import work_item_workflow, workflow_model
from tranibor_api.temporal import get_temporal, train_id
from tranibor_core import utcnow
from tranibor_core.dsl.trains import train_after_merge, train_approval, train_env

#: What `received_events` records for a merged work item.
BOARDED = "boarded"


async def board_merged_item(session: Any, project: Any, item: Any, event: Any) -> str:
    """The merged ticket boards the train that ITS workflow takes after the merge, with the approval
    it requires there (ADR 0041). Every merged ticket used to board the PROD train: a study that
    merges an ADR went to production, a careful delivery skipped its staging."""
    workflow = workflow_model(await work_item_workflow(session, item))
    train = train_after_merge(workflow)
    if train is None:
        return BOARDED
    # The PR's labels AND the ticket's: a `hotfix` ticket takes the express lane, whether this
    # webhook or the interpreter boards it first (#279).
    merged_labels = [*event.payload.get("labels", []), *(item.documents or {}).get("labels", [])]
    await get_temporal().signal(
        train_id(project.slug, train_env(train)),
        "merged",
        {
            "work_item_key": item.tracker_key,
            "sha": event.payload.get("sha", ""),
            "merged_at": utcnow().isoformat(),  # start bound of the delivery delay
            "risk": item.risk,
            "labels": list(dict.fromkeys(merged_labels)),
            "pr_url": event.payload.get("pr_url"),
            "title": item.title,
            "approval": dict(train_approval(workflow, train)),
        },
    )
    return BOARDED
