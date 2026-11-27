# SPDX-License-Identifier: Apache-2.0
"""A declared history for a demonstration (ADR 0216): `schemas/demo-history.schema.json` as a model.

The document describes, for an existing project, the work items of a demonstration, the states they went
through, the runs and costs of their agents, the decisions people took and the findings raised.
`POST /projects/{id}/demo-history` takes it in where the instance opted in (`TRANIBOR_DEMO_HISTORY_IMPORT`);
what it writes is marked as imported.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from .open_lists import FINDING_TYPE_PATTERN
from .workflow import Strict

#: The external id of an object of the document: what makes the import idempotent.
ExternalId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]


class DemoPrice(Strict):
    """USD per million tokens, as the document declares it for one model."""

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    cached_input_usd_per_mtok: float | None = Field(default=None, ge=0)


class DemoStateEntry(Strict):
    state: str = Field(min_length=1, max_length=64)
    at: datetime
    by: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class DemoTokens(Strict):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cached: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _cached_is_part_of_input(self) -> DemoTokens:
        if self.cached > self.input:
            raise ValueError("`cached` is a part of `input`: it cannot be larger")
        return self


class DemoGate(Strict):
    """A gate's verdict on the run, as the live step's journal records it (`gate.outcome`)."""

    name: str = Field(min_length=1, max_length=64)
    passed: bool
    detail: str | None = Field(default=None, max_length=2000)


class DemoRun(Strict):
    id: ExternalId
    stage: str = Field(min_length=1, max_length=32)
    agent: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    attempt: int = Field(default=1, ge=1)
    status: Literal["succeeded", "failed", "cancelled", "timed_out"]
    started_at: datetime
    duration_s: float | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=200)
    tokens: DemoTokens | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    summary: str | None = Field(default=None, max_length=2000)
    #: What the step returned (`StageResult.outputs`): its declared outputs, a report, cited sources.
    outputs: dict[str, Any] = Field(default_factory=dict)
    #: What the step proved (`StageResult.evidence`): facts, tests run.
    evidence: dict[str, Any] | None = None
    #: The verdicts of the step's gates, in order.
    gates: list[DemoGate] = Field(default_factory=list, max_length=50)


class DemoDecision(Strict):
    id: ExternalId
    kind: Literal["approval", "question", "scope_change"]
    summary: str = Field(min_length=1, max_length=2000)
    requested_at: datetime | None = None
    at: datetime
    by: str = Field(max_length=200)
    approved: bool = True
    answer: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)


class DemoPending(Strict):
    """A request that still waits for a person: the inbox lists it, and an approver decides it.

    Deciding it records the decision and closes the request; no workflow runs the imported work item, so
    nothing follows from it: no agent runs, nothing is written outside the platform.
    """

    id: ExternalId
    kind: Literal["approval", "question", "scope_change"]
    summary: str = Field(min_length=1, max_length=2000)
    requested_at: datetime
    due_at: datetime | None = None

    @model_validator(mode="after")
    def _due_after_asked(self) -> DemoPending:
        if self.due_at is not None and self.due_at <= self.requested_at:
            raise ValueError("`due_at` comes after `requested_at`")
        return self


class DemoAction(Strict):
    """The governed action a transition of the workflow proposes (ADR 0035), proposed for the work item by the
    product's own service: its title, parameters and approvers rendered from the work item's fields.

    `pending`: it waits for its approvers; deciding it records the decision and plays nothing, since no
    workflow runs an imported work item. `succeeded`: it was approved by `decided_by` and its effects are
    recorded as imported, never played."""

    transition: str = Field(min_length=1, max_length=64)
    status: Literal["pending", "succeeded"] = "pending"
    at: datetime
    decided_by: str | None = Field(default=None, max_length=200)
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def _decided_when_succeeded(self) -> DemoAction:
        if self.status == "succeeded" and (self.decided_by is None or self.decided_at is None):
            raise ValueError("a `succeeded` action names `decided_by` and `decided_at`")
        if self.status == "pending" and (self.decided_by is not None or self.decided_at is not None):
            raise ValueError("a `pending` action is not decided yet: no `decided_by` nor `decided_at`")
        if self.decided_at is not None and self.decided_at < self.at:
            raise ValueError("`decided_at` comes after `at`")
        return self


class DemoFinding(Strict):
    id: ExternalId
    title: str = Field(min_length=3, max_length=200)
    #: A name, read by the list's pattern alone: the import judges it against the project's workflow, whose
    #: own types (ADR 0217) the contract cannot know, as the finding routes do.
    type: str = Field(pattern=FINDING_TYPE_PATTERN)
    severity: Literal["low", "medium", "high", "critical"]
    evidence: str = Field(min_length=1)
    suggested_fix: str | None = None
    estimate: Literal["S", "M", "L"] | None = None
    status: Literal["pending", "dismissed"] = "pending"
    run: ExternalId | None = None
    at: datetime | None = None


class DemoWorkItem(Strict):
    id: ExternalId
    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    url: str | None = Field(default=None, max_length=500)
    size: Literal["S", "M", "L", "XL"] | None = None
    risk: Literal["low", "medium", "high", "critical"] | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = Field(default=None, max_length=200)
    created_at: datetime
    state: str | None = Field(default=None, min_length=1, max_length=64)
    history: list[DemoStateEntry] = Field(default_factory=list, max_length=100)
    runs: list[DemoRun] = Field(default_factory=list, max_length=100)
    decisions: list[DemoDecision] = Field(default_factory=list, max_length=100)
    findings: list[DemoFinding] = Field(default_factory=list, max_length=100)
    #: What the work item waits for now, if a person must decide: at most one, as a live ticket.
    pending: DemoPending | None = None
    #: The governed actions its transitions proposed (ADR 0035), at most one per transition.
    actions: list[DemoAction] = Field(default_factory=list, max_length=10)


class DemoHistory(Strict):
    """The document `POST /projects/{id}/demo-history` takes."""

    #: `tranibor/DemoHistory/v1`; the former `choregos/DemoHistory/v1` is accepted until 0.32.0 (ADR 0226).
    schema_: Literal["tranibor/DemoHistory/v1", "choregos/DemoHistory/v1"] = Field(alias="schema")
    workflow: str | None = Field(default=None, min_length=1, max_length=64)
    prices: dict[str, DemoPrice] = Field(default_factory=dict)
    work_items: list[DemoWorkItem] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
