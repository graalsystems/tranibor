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

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from .open_lists import FINDING_TYPES
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


class DemoFinding(Strict):
    id: ExternalId
    title: str = Field(min_length=3, max_length=200)
    type: Annotated[str, AfterValidator(FINDING_TYPES.check)]
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


class DemoHistory(Strict):
    """The document `POST /projects/{id}/demo-history` takes."""

    schema_: Literal["choregos/DemoHistory/v1"] = Field(alias="schema")
    workflow: str | None = Field(default=None, min_length=1, max_length=64)
    prices: dict[str, DemoPrice] = Field(default_factory=dict)
    work_items: list[DemoWorkItem] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
