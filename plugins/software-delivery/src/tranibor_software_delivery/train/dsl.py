# SPDX-License-Identifier: Apache-2.0
"""What a workflow asks of the train a merged work item takes (ADR 0041; ADR 0181, round D, S83-05k).

The product review of 2026-10-07 found it: `train: {env: prod, approval: captain}` was written in the
shipped workflows and read by nobody. These functions say, from the work item's PINNED workflow, which
train it boards once its pull request merges (`merged.board_merged_item`). What the interpreter hands a
carrier when a transition boards it (`boarding_env`, `boarding_approval`) is the core's carrier seam's.
"""

from __future__ import annotations

from tranibor_contracts import SystemActor, Workflow
from tranibor_contracts.workflow import Transition, transition_effect
from tranibor_core.carriers import carrier_of


def takes_the_train(transition: Transition) -> bool:
    return carrier_of(transition) == "release_train"


def train_after_merge(workflow: Workflow) -> Transition | None:
    """The train a work item takes right after its PR merges, or `None` when it takes none:
    a study that merges an ADR goes nowhere, a prudent delivery goes through staging first.
    The GitHub webhook used to ship every merged work item to PROD, whatever its workflow."""
    for merge in workflow.transitions:
        actor = workflow.actors.get(merge.by or "")
        if not isinstance(actor, SystemActor) or transition_effect(merge) != "merge_pr":
            continue
        for follow_up in workflow.transitions:
            if follow_up.from_ == merge.to and takes_the_train(follow_up):
                return follow_up
    return None
