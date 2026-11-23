# SPDX-License-Identifier: Apache-2.0
"""`default-simple` from end to end: agents, human, system, pull request, CI, review, train.

Moved from `apps/orchestrator/tests/test_interpreter.py` (S83-05n): the traversal opens and merges a
pull request and boards the release train, the `software-delivery` plugin's. The interpreter's own
mechanisms stay in the core suite, on a workflow of the core's.
"""

from __future__ import annotations

from typing import Any

import pytest
from tranibor_testkit.interpreter import pr_ref, scripted, start, wait_state
from tranibor_testkit.orchestrator import Fixture, stage_result

pytestmark = pytest.mark.asyncio


async def test_default_simple_full_traversal(setup: Fixture, temporal_env: Any, worker_factory: Any) -> None:
    """A ticket goes through `default-simple` from end to end: agents, human, system, train."""
    scripted(
        setup.adapters,
        stage_result("spec written", outputs={"size": "M", "risk": "low", "spec_markdown": "## Spec"}),
        stage_result(
            "implemented", artifacts={"branch": "choregos/123", "commits": ["fix(orders): credit notes"]}
        ),
        stage_result("verified"),
    )
    setup.adapters.cd.set_health("billing-api", "Healthy")

    async with worker_factory():
        handle = await start(temporal_env, setup)

        # 1. the `refine` agent ran, the human validation is requested
        await wait_state(handle, "awaiting_spec_approval")
        await handle.signal(
            "human_decision", {"approved": True, "decided_by": "augustin", "kind": "approval"}
        )

        # 2. implementation, verification, PR: CI and review arrive by events
        await wait_state(handle, "pr_open")
        await handle.signal(
            "inbound",
            {
                "type": "scm.check.completed",
                "source": "github",
                "delivery_id": "d1",
                "payload": {"conclusion": "success"},
            },
        )
        setup.adapters.scm.set_checks(pr_ref(setup), "success")
        setup.adapters.scm.submit_review(pr_ref(setup), "marie", "approved")
        await handle.signal(
            "inbound",
            {
                "type": "scm.pr.review_submitted",
                "source": "github",
                "delivery_id": "d2",
                "payload": {"state": "approved"},
            },
        )

        # 3. the train announces the successful deployment
        await wait_state(handle, "merged")
        await handle.signal(
            "inbound",
            {
                "type": "cd.rollout.completed",
                "source": "argocd",
                "delivery_id": "d3",
                "payload": {"env": "prod"},
            },
        )
        outcome = await handle.result()

    assert outcome["state"] == "deployed_prod"
    assert outcome["cost_usd"] >= 0
    comment = setup.adapters.tracker.status_comment(setup.tracker_key)
    assert comment is not None and "Tranibor — progress" in comment
    assert "agent refine" in comment and "agent implement" in comment
