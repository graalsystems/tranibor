# SPDX-License-Identifier: Apache-2.0
"""Memory, A/B, costs, DORA, cross review."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from .base import Dto, PageMeta


class MemoryDto(Dto):
    id: str
    kind: str
    subject: str
    content: str
    score: float | None = None
    status: str = "active"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    proposed_by: str | None = None


class MemoryPage(Dto):
    """One page of a memory's review queue, oldest first (ADR 0206)."""

    items: list[MemoryDto]
    meta: PageMeta


class MemoryDecision(Dto):
    id: str
    action: Literal["accept", "reject"]
    reason: str | None = None


class MemoryReimport(Dto):
    sources: list[str] = Field(default_factory=list)


class CrossBackendArm(Dto):
    """The reviews done by the same backend as the implementer, or by another."""

    reviews: int = 0
    caught: int = 0
    catch_rate: float | None = None
    backends: list[str] = Field(default_factory=list)


class CrossBackendReport(Dto):
    """Does multi-backend catch what a single backend lets through? (S13-03)"""

    since: datetime
    cross_backend_required: bool = False
    same_backend: CrossBackendArm = Field(default_factory=CrossBackendArm)
    other_backend: CrossBackendArm = Field(default_factory=CrossBackendArm)
    verdict: str = "not enough data"
    detail: str = ""


class MemoryAbGroup(Dto):
    """One of the two arms of the A/B: the projects with, or without, a context pack."""

    projects: list[str] = Field(default_factory=list)
    tickets: int = 0
    #: The share of the work items accepted without a rework loop (ADR 0181 C1).
    first_pass_acceptance_rate: float | None = None
    cost_per_ticket_usd: float | None = None


class MemoryAbReport(Dto):
    """Proof before dependency: does memory pay off? (docs/plan/04, decision 4)"""

    org: str
    weeks: int = 4
    since: datetime | None = None
    with_memory: MemoryAbGroup = Field(default_factory=MemoryAbGroup)
    without_memory: MemoryAbGroup = Field(default_factory=MemoryAbGroup)
    verdict: str
    detail: str = ""


class CostRow(Dto):
    key: str
    cost_usd: float = 0.0
    cost_eur: float = 0.0
    #: The line in the report's `currency` (ADR 0214): the ledger's EUR or USD column, or USD times the
    #: organisation's declared rate.
    cost_display: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    #: The part of `tokens_cache_write` written with the 1-hour TTL (S4-12, ADR 0200).
    tokens_cache_write_1h: int = 0
    runs: int = 0
    #: The part of `cost_usd` that a demonstration history import wrote (ADR 0216): never measured.
    imported_usd: float = 0.0


class CostTotal(Dto):
    cost_usd: float = 0.0
    cost_eur: float = 0.0
    #: The total in the report's `currency` (ADR 0214).
    cost_display: float = 0.0
    #: Always USD: budgets are set, enforced and alerted in USD (ADR 0214).
    budget_usd: float | None = None
    #: The report's tokens, its rows' summed (ADR 0141 §1); `tokens_cached` is part of `tokens_in`.
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    #: The part of `tokens_cache_write` written with the 1-hour TTL (S4-12, ADR 0200).
    tokens_cache_write_1h: int = 0
    #: The part of `cost_usd` that a demonstration history import wrote (ADR 0216): never measured.
    imported_usd: float = 0.0


class CostFx(Dto):
    """How a report's `cost_display` was obtained (ADR 0214)."""

    #: True when it is USD converted at the organisation's declared rate; False for the ledger's own column.
    declared: bool = False
    #: The declared units of `currency` per 1 USD.
    rate: float | None = None
    #: The UTC date the organisation set that rate.
    set_on: date | None = None


class CostReport(Dto):
    group_by: str
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    fx: CostFx = Field(default_factory=CostFx)
    rows: list[CostRow] = Field(default_factory=list)
    total: CostTotal = Field(default_factory=CostTotal)


class BudgetAlertDto(Dto):
    """A budget that crossed a threshold (S29-04): what waits in the inbox until someone has seen it."""

    id: str
    scope: str
    subject: str
    period: str
    period_key: str
    #: The share of the budget crossed, in percent: 80, 100.
    threshold: int
    spent_usd: float
    budget_usd: float
    status: str
    created_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    #: Whether the person reading may acknowledge it (project owner or above, S29-04c): the console
    #: offers the gesture only to them. The acknowledge endpoint checks again.
    can_acknowledge: bool = False


class DoraMetric(Dto):
    """One of the four DORA measures, with its tier (`elite`...`low`) and its sample."""

    value: float | None = None
    unit: str
    level: str = "unknown"
    sample: int = 0


class DoraReport(Dto):
    """The four DORA measures, computed on the deployments actually recorded."""

    env: str = "prod"
    since: datetime
    until: datetime
    deployments: int = 0
    deployment_frequency: DoraMetric
    lead_time: DoraMetric
    change_failure_rate: DoraMetric
    time_to_restore: DoraMetric


class BudgetAlertPage(Dto):
    """One page of budget alerts, newest first (ADR 0206)."""

    items: list[BudgetAlertDto]
    meta: PageMeta


class DemoHistoryCounts(Dto):
    """What a demonstration history import wrote, or found already written (ADR 0216)."""

    work_items: int = 0
    runs: int = 0
    decisions: int = 0
    findings: int = 0
    #: Requests that still wait for a person: the inbox lists them.
    pending: int = 0


class DemoHistoryImport(Dto):
    created: DemoHistoryCounts
    unchanged: DemoHistoryCounts
    #: The cost the created runs carry, in USD: declared by the document, never measured.
    cost_usd: float = 0.0


class DemoHistoryRemoval(Dto):
    """What removing a project's imported demonstration history deleted (ADR 0216): marked rows only."""

    work_items: int = 0
    runs: int = 0
    #: Human requests of the imported work items, decided or still waiting.
    requests: int = 0
    findings: int = 0
    cost_lines: int = 0
    #: The declared cost of the removed cost lines, in USD.
    cost_usd: float = 0.0
