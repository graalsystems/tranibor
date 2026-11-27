# SPDX-License-Identifier: Apache-2.0
"""The platform's Prometheus metrics: computed from the database, exposed by the API.

WHY FROM THE DATABASE
---------------------
The chart shipped a ServiceMonitor, four alerts and six dashboards that read sixteen `choregos_*`
series that no process emitted (inventory of 2026-09-24). The classic way, in-memory counters in
every process, would have given per-replica numbers to stitch together, and would have required an
HTTP server in the orchestrator. Here, everything that counts (runs, costs, tickets, refusals) is
ALREADY in the database: one truth, the same on every API replica, refreshed every few seconds and
served on `/metrics`. Only `http_requests_total` is a real process counter.

WHAT IT DOES NOT DO
-------------------
No traces. This module gives the series that the dashboards read; the inventory `NAMES` is what a
test compares with the dashboards and the alerts, so that a panel never again cites a metric that
does not exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client import Counter as PromCounter
from prometheus_client.core import GaugeMetricFamily, HistogramMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from tranibor_core import utcnow

from . import __version__
from .edition import current as current_edition
from .logging import get_logger
from .metric_series import (  # noqa: F401 - the names `metrics` has always exposed
    _SERIES,
    NAMES,
    PLUGIN_SERIES,
    declare_series,
    emitted_names,
    forget_series,
    former_name,
)
from .services.personal_spaces import PERSONAL, is_personal

logger = get_logger("choregos.metrics")

START_SECONDS = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
DELIVERY_SECONDS = (3600.0, 4 * 3600.0, 8 * 3600.0, 24 * 3600.0, 3 * 86400.0, 7 * 86400.0, 30 * 86400.0)
registry = CollectorRegistry()
http_requests = PromCounter(
    "http_requests_total",
    "HTTP requests received by the API",
    ["method", "route", "status"],
    registry=registry,
)


def _buckets(values: Iterable[float], bounds: tuple[float, ...]) -> tuple[list[tuple[str, float]], float]:
    """Cumulative counts per bound, as Prometheus expects them, and the sum."""
    items = list(values)
    cumulative = [(str(b), float(sum(1 for v in items if v <= b))) for b in bounds]
    cumulative.append(("+Inf", float(len(items))))
    return cumulative, float(sum(items))


class Snapshot:
    """What the database said at the last refresh. Empty until one has happened."""

    def __init__(self) -> None:
        self.runs_active = 0.0
        self.runs_total: dict[str, float] = {}
        self.run_start: tuple[list[tuple[str, float]], float] = ([], 0.0)
        self.cost: dict[tuple[str, str, str], float] = {}
        self.daily_cost: dict[str, float] = {}
        self.daily_budget: dict[str, float] = {}
        self.first_pass: dict[str, float] = {}
        self.lead_time: tuple[list[tuple[str, float]], float] = ([], 0.0)
        #: The families the extensions' series rendered at this refresh.
        self.extension_families: list[Any] = []
        self.denials: dict[str, float] = {}
        self.memory_pending = 0.0
        self.context_pack_empty: dict[str, float] = {}
        self.outpost_poll_conflicts: dict[str, float] = {}
        #: By sink: the audit entries written and not yet accepted by it, and when its cursors last moved.
        self.audit_export_lag: dict[str, float] = {}
        self.audit_export_progress: dict[str, float] = {}
        self.refreshed_at: float | None = None


class _Current:
    """The latest snapshot. A holder rather than a `global`: it is replaced in one piece."""

    snapshot = Snapshot()


class PlatformCollector(Collector):
    """Renders the snapshot as series. No side effect, no database: reading is instantaneous."""

    def collect(self) -> Iterable[Any]:
        s = _Current.snapshot
        # The edition does not come from the database: it is a property of the process. An `_info`
        # series at 1 with the edition as a label is the usual form: it joins the others, so a
        # dashboard can say "which edition produced this figure". Without it, the only way to know
        # what an operator is connected to is to call `/edition` by hand, and nothing says it in
        # the history of the metrics.
        edition = GaugeMetricFamily(
            "tranibor_edition_info", "Edition that is running (ADR 0024)", labels=["edition", "version"]
        )
        edition.add_metric([current_edition(), __version__], 1.0)
        yield edition
        yield GaugeMetricFamily("tranibor_runs_active", "Runs queued or running", value=s.runs_active)
        g = GaugeMetricFamily("tranibor_runs_total", "Runs by final state", labels=["status"])
        for status, n in s.runs_total.items():
            g.add_metric([status], n)
        yield g
        yield HistogramMetricFamily(
            "tranibor_run_start_seconds",
            "From the creation of a run to its start (last 30 days)",
            buckets=s.run_start[0],
            sum_value=s.run_start[1],
        )
        g = GaugeMetricFamily(
            "tranibor_cost_usd_total", "Spend in the ledger, USD", labels=["project", "stage", "model"]
        )
        for (project, stage, model), v in s.cost.items():
            g.add_metric([project, stage, model], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_project_daily_cost_usd", "Spend of the day (UTC) per project", labels=["project"]
        )
        for project, v in s.daily_cost.items():
            g.add_metric([project], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_project_daily_budget_usd", "Daily budget of the policy", labels=["project"]
        )
        for project, v in s.daily_budget.items():
            g.add_metric([project], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_first_pass_merge_rate", "Share of PRs merged on the first pass", labels=["project"]
        )
        for project, v in s.first_pass.items():
            g.add_metric([project], v)
        yield g
        yield HistogramMetricFamily(
            "tranibor_lead_time_seconds",
            "From the creation of a ticket to its closing (last 90 days)",
            buckets=s.lead_time[0],
            sum_value=s.lead_time[1],
        )
        g = GaugeMetricFamily(
            "tranibor_permission_denials_total", "Permission requests refused", labels=["project"]
        )
        for project, v in s.denials.items():
            g.add_metric([project], v)
        yield g
        yield GaugeMetricFamily(
            "tranibor_memory_pending", "Memory facts awaiting validation", value=s.memory_pending
        )
        g = GaugeMetricFamily(
            "tranibor_context_pack_empty_total", "Runs started with an empty context pack", labels=["project"]
        )
        for project, v in s.context_pack_empty.items():
            g.add_metric([project], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_outpost_poll_conflicts_total",
            "Alerts raised for a second poll under one outpost instance's key (ADR 0148)",
            labels=["org"],
        )
        for org, v in s.outpost_poll_conflicts.items():
            g.add_metric([org], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_audit_export_lag_entries",
            "Audit entries written and not yet accepted by the SIEM sink (ADR 0207)",
            labels=["sink"],
        )
        for sink, v in s.audit_export_lag.items():
            g.add_metric([sink], v)
        yield g
        g = GaugeMetricFamily(
            "tranibor_audit_export_progress_timestamp_seconds",
            "When the SIEM sink's cursors last moved (ADR 0207)",
            labels=["sink"],
        )
        for sink, v in s.audit_export_progress.items():
            g.add_metric([sink], v)
        yield g
        # The series the installed extensions declared (`declare_series`), as rendered at the last refresh.
        yield from s.extension_families


registry.register(PlatformCollector())


class _WithFormerNames:
    """The registry, each `tranibor_*` family followed by its `choregos_*` twin, the same samples under the
    former name (ADR 0226, R9b; `metric_series.PREFIX` says why and what it costs). The twin is built at
    exposure, from the family itself: a process counter and a snapshot gauge are counted once and served
    twice, so the two names can never disagree."""

    def __init__(self, source: CollectorRegistry) -> None:
        self.source = source

    def collect(self) -> Iterable[Any]:
        for family in self.source.collect():
            yield family
            twin_name = former_name(family.name)
            if twin_name is None:
                continue
            twin = copy.copy(family)
            twin.name = twin_name
            twin.samples = [
                sample._replace(name=former_name(sample.name) or sample.name) for sample in family.samples
            ]
            yield twin


def expose() -> bytes:
    return generate_latest(_WithFormerNames(registry))


async def refresh() -> Snapshot:
    """Recomputes the snapshot from the database. One pass: about ten bounded queries."""
    from .db.models import (
        AuditChainHead,
        AuditExportCursor,
        AuditLog,
        CostLedger,
        MemoryFact,
        Organization,
        Project,
        Run,
        RunEvent,
        WorkItem,
    )
    from .db.session import session_scope
    from .services import effective_policy, first_pass_merge_rate
    from .services.outposts import POLL_CONFLICT

    now = utcnow()
    fresh = Snapshot()
    # A demonstration history's rows were never measured (ADR 0216): no gauge counts them.
    measured_run = Run.imported.is_(False)
    measured_cost = CostLedger.imported.is_(False)
    async with session_scope(orgs="*") as session:
        everything = list((await session.execute(select(Project))).scalars())
        # A personal space is one person's (ADR 0134): never a label of its own, which would name a
        # person and grow with every user; their spend is summed under `personal`.
        slug = {p.id: (PERSONAL if is_personal(p) else p.slug) for p in everything}
        projects = {p.id: p for p in everything if not is_personal(p)}

        active = await session.execute(
            select(func.count()).select_from(Run).where(Run.status.in_(["queued", "running"]), measured_run)
        )
        fresh.runs_active = float(active.scalar_one())
        by_status = await session.execute(
            select(Run.status, func.count()).where(measured_run).group_by(Run.status)
        )
        fresh.runs_total = {str(st): float(n) for st, n in by_status.all()}

        since = now - timedelta(days=30)
        started = await session.execute(
            select(Run.created_at, Run.started_at).where(
                Run.started_at.is_not(None), Run.created_at >= since, measured_run
            )
        )
        durations = [max(0.0, (_aware(s) - _aware(c)).total_seconds()) for c, s in started.all() if s and c]
        fresh.run_start = _buckets(durations, START_SECONDS)

        costs = await session.execute(
            select(
                CostLedger.project_id, CostLedger.stage_role, CostLedger.model, func.sum(CostLedger.cost_usd)
            )
            # An organisation-level line has no project label.
            .where(CostLedger.project_id.is_not(None), measured_cost)
            .group_by(CostLedger.project_id, CostLedger.stage_role, CostLedger.model)
        )
        fresh.cost = {
            (slug.get(str(pid), "?"), str(stage or ""), str(model or "")): float(total or 0.0)
            for pid, stage, model, total in costs.all()
        }
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = await session.execute(
            select(CostLedger.project_id, func.sum(CostLedger.cost_usd))
            .where(CostLedger.ts >= midnight, CostLedger.project_id.is_not(None), measured_cost)
            .group_by(CostLedger.project_id)
        )
        fresh.daily_cost = {slug.get(str(pid), "?"): float(total or 0.0) for pid, total in today.all()}
        for pid, project in projects.items():
            policy = await effective_policy(session, pid)
            budget = policy.budgets.daily_project_usd
            if budget:
                fresh.daily_budget[project.slug] = float(budget)
            rate = await first_pass_merge_rate(session, pid)
            if rate is not None:
                fresh.first_pass[project.slug] = float(rate)

        closed = await session.execute(
            select(WorkItem.created_at, WorkItem.closed_at).where(
                WorkItem.closed_at.is_not(None),
                WorkItem.closed_at >= now - timedelta(days=90),
                WorkItem.imported.is_(False),
            )
        )
        fresh.lead_time = _buckets(
            [max(0.0, (_aware(f) - _aware(c)).total_seconds()) for c, f in closed.all() if f and c],
            DELIVERY_SECONDS,
        )

        denials = await session.execute(
            select(Run.project_id, func.count())
            .select_from(RunEvent)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                RunEvent.type == "session/request_permission",
                RunEvent.payload["allowed"].as_boolean().is_(False),
                measured_run,
            )
            .group_by(Run.project_id)
        )
        fresh.denials = {slug.get(str(pid), "?"): float(n) for pid, n in denials.all()}

        pending = await session.execute(
            select(func.count()).select_from(MemoryFact).where(MemoryFact.status == "pending")
        )
        fresh.memory_pending = float(pending.scalar_one())

        empty: Counter[str] = Counter()
        packs = await session.execute(
            select(Run.project_id, Run.context_pack).where(Run.created_at >= since, measured_run)
        )
        for pid, pack in packs.all():
            if pack is not None and not (pack.get("facts") or pack.get("items") or pack.get("snippets")):
                empty[slug.get(str(pid), "?")] += 1
        fresh.context_pack_empty = {k: float(v) for k, v in empty.items()}

        # What an alert rule reads: the door raises one audit event for a second poll under a key that
        # already has one waiting, which is what a stolen key looks like (ADR 0148 §7).
        conflicts = await session.execute(
            select(Organization.slug, func.count())
            .select_from(AuditLog)
            .join(Organization, Organization.id == AuditLog.org_id)
            .where(AuditLog.action == POLL_CONFLICT)
            .group_by(Organization.slug)
        )
        fresh.outpost_poll_conflicts = {str(org): float(n) for org, n in conflicts.all()}

        # The export's lag, from the cursors and the chains' heads, the same on every replica (ADR 0207).
        cursors = await session.execute(
            select(
                AuditExportCursor.sink,
                func.coalesce(func.sum(AuditChainHead.seq - AuditExportCursor.seq), 0),
                func.max(AuditExportCursor.updated_at),
            )
            .select_from(AuditExportCursor)
            .join(AuditChainHead, AuditChainHead.chain == AuditExportCursor.chain, isouter=True)
            .group_by(AuditExportCursor.sink)
        )
        for sink, behind, moved in cursors.all():
            fresh.audit_export_lag[str(sink)] = float(max(0, behind or 0))
            if moved is not None:
                fresh.audit_export_progress[str(sink)] = _aware(moved).timestamp()

    fresh.extension_families = await _extension_series(list(slug.values()))
    fresh.refreshed_at = now.timestamp()
    _Current.snapshot = fresh
    return fresh


async def _extension_series(slugs: list[str]) -> list[Any]:
    """The families of the extensions' series. A refresh that fails leaves its series missing; the API
    lives."""
    families: list[Any] = []
    for plugin, _names, refresh_series in list(_SERIES):
        try:
            families.extend(await refresh_series(slugs))
        except Exception as exc:
            logger.warning("extension series not refreshed", plugin=plugin, error=str(exc)[:200])
    return families


def _aware(moment: Any) -> Any:
    from datetime import UTC

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def refresh_loop(interval_s: float) -> None:
    """Background task: a failed refresh does not kill the API, it shows in `refreshed_at`."""
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("metrics refresh failed", error=str(exc)[:200])
        await asyncio.sleep(interval_s)


def _stub_for_type_checkers() -> None:  # pragma: no cover
    _ = defaultdict, contextlib
