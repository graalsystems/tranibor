# SPDX-License-Identifier: Apache-2.0
"""Workflows the core runs by itself, for the tests of its mechanisms (ADR 0181, round D, S83-05n).

The orchestrator's `setup` project has a repository and no template: with the `software-delivery` plugin's
extension installed it gets `default-simple`, without it `default-request` (S83-05d). A test of a core
mechanism (an escalation, a resumption, a budget, a refusal) that reads `default-simple`'s states therefore
read the plugin's workflow. `CORE_SIMPLE` is the same front half, step for step: `t-refine` and its human
approval, `t-implement`, `t-verify`, under the same ids, roles, attempts and defaults; where
`default-simple` opens and merges a pull request and boards the release train, it ends with a step of the
core (`t-deliver`, a system step on `evidence_present`). It names no gate, step effect or carrier of a
plugin, so it is published and runs the same with or without the plugins' extensions.

`use_workflow` publishes a workflow on the `setup` project, makes it the project's default and pins the
work item to it, as a template would have.
"""

from __future__ import annotations

from typing import Any

#: `default-simple`'s front half, on the core's gates only (see the module's docstring).
CORE_SIMPLE = """\
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: core-simple
  version: 1
  description: Refinement → human approval → implementation → verification → delivery, on the core's gates.
actors:
  refiner: { type: agent, role: refine, model: "profile:standard" }
  owner: { type: human, group: product-owners, sla_hours: 24 }
  dev: { type: agent, role: implement, model: "profile:by_size" }
  checker: { type: agent, role: verify, model: "profile:standard" }
  desk: { type: system }
states:
  inbox: { display: To triage, kind: wait, tracker: { status: Todo, label: "choregos:inbox" } }
  refining: { display: Refining, tracker: { status: Refining } }
  awaiting_spec_approval: { display: Spec to approve, kind: wait, tracker: { status: Needs review } }
  ready: { display: Ready, tracker: { status: Ready } }
  in_progress: { display: In progress, tracker: { status: In progress } }
  verifying: { display: Verifying, tracker: { status: Verifying } }
  done: { display: Done, terminal: true, tracker: { status: Done } }
  needs_human: { display: Needs a human, kind: wait, tracker: { status: Blocked } }
  abandoned: { display: Abandoned, terminal: true, tracker: { status: Cancelled } }
transitions:
  - id: t-refine
    from: inbox
    to: awaiting_spec_approval
    by: refiner
    outputs: [spec_markdown, size, risk]
    on_fail: { to: inbox, max_attempts: 2, escalate_to: needs_human }
  - id: t-approve-spec
    from: awaiting_spec_approval
    to: ready
    by: owner
    on_reject: refining
    timeout_hours: 72
  - id: t-refine-again
    from: refining
    to: awaiting_spec_approval
    by: refiner
    on_fail: { to: refining, max_attempts: 2, escalate_to: needs_human }
  - id: t-implement
    from: ready
    to: in_progress
    by: dev
    inputs: [spec_markdown, allowed_paths]
    gates: [evidence_present]
    on_fail: { to: ready, max_attempts: 2, escalate_to: needs_human }
  - id: t-verify
    from: in_progress
    to: verifying
    by: checker
    gates: [evidence_present]
    inputs: [spec_markdown]
    on_fail: { to: in_progress, max_attempts: 2, escalate_to: needs_human }
  - id: t-deliver
    from: verifying
    to: done
    by: desk
    gates: [evidence_present]
    on_fail: { to: verifying, max_attempts: 2, escalate_to: needs_human }
  - id: t-abandon
    from: needs_human
    to: abandoned
    by: owner
    timeout_hours: 168
defaults:
  from_any_agent_state:
    on_question: needs_human
    on_budget_exceeded: needs_human
    on_timeout: needs_human
  needs_human:
    on_answer: resume
    on_abandon: abandoned
"""


async def use_workflow(setup: Any, source: str = CORE_SIMPLE) -> Any:
    """Publishes `source` on the `setup` project, as its default, and pins the work item to it; returns
    the published definition."""
    from tranibor_api.db.models import Project, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.rbac import SYSTEM
    from tranibor_api.services.definitions import publish_workflow

    async with session_scope() as session:
        project = await session.get(Project, setup.project_id)
        assert project is not None
        definition = await publish_workflow(session, SYSTEM, project, source)
        project.default_workflow = definition.name
        item = await session.get(WorkItem, setup.work_item_id)
        assert item is not None
        item.workflow_def_id = definition.id
        return definition


__all__ = ["CORE_SIMPLE", "use_workflow"]
