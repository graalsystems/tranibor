# SPDX-License-Identifier: Apache-2.0
"""The release train's Prometheus series (ADR 0181, round D), declared to the API's `/metrics` through
`metrics.declare_series`: `choregos_releases_total` (releases by state), `choregos_train_frozen` and
`choregos_train_status` (one sample per project and environment whose train answers its query). The
names are the dashboards' and the alerts', unchanged."""

from __future__ import annotations

import asyncio
from typing import Any

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from tranibor_api.db.models import Release
from tranibor_api.db.session import session_scope
from tranibor_api.logging import get_logger
from tranibor_api.temporal import get_temporal, train_id

logger = get_logger("choregos.metrics")

NAMES = ("choregos_releases_total", "choregos_train_frozen", "choregos_train_status")
ENVIRONMENTS = ("dev", "staging", "prod")
#: The query the `ReleaseTrain` workflow answers (`workflow.py`, `status_query`), as the trains route asks it.
TRAIN_STATUS_QUERY = "status_query"
#: The dashboards show the state of a train as a number, keyed by the `status` the train's `status_query`
#: returns (`ReleaseStatus`). "staged" was never a status: a train at staging fell to -1 (S26-03).
TRAIN_STATES = {
    "collecting": 0,
    "departing": 1,
    "staging": 2,
    "awaiting_approval": 3,
    "promoting": 4,
    "verifying": 5,
}


async def refresh(project_slugs: list[str]) -> list[Any]:
    """The three families, from the database and from each train's query."""
    async with session_scope(orgs="*") as session:
        rows = await session.execute(select(Release.status, func.count()).group_by(Release.status))
        releases = {str(status): float(n) for status, n in rows.all()}
    families: list[Any] = []
    g = GaugeMetricFamily("choregos_releases_total", "Releases by state", labels=["status"])
    for status, n in releases.items():
        g.add_metric([status], n)
    families.append(g)
    frozen, states = await _trains(project_slugs)
    g = GaugeMetricFamily("choregos_train_frozen", "1 if the train is frozen", labels=["project", "env"])
    for (project, env), v in frozen.items():
        g.add_metric([project, env], v)
    families.append(g)
    g = GaugeMetricFamily(
        "choregos_train_status",
        "State of the train (0 collecting ... 5 verifying)",
        labels=["project", "env"],
    )
    for (project, env), v in states.items():
        g.add_metric([project, env], v)
    families.append(g)
    return families


async def _trains(slugs: list[str]) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """The state of the trains lives in Temporal: one `status_query` per (project, environment), bounded.

    S26-03: it asked for `status`, a query the train does not define, and read `state`, a field it
    does not return; the failure was swallowed below, so the train series were always empty.
    """
    frozen: dict[tuple[str, str], float] = {}
    states: dict[tuple[str, str], float] = {}
    temporal = get_temporal()
    for project in slugs:
        for env in ENVIRONMENTS:
            try:
                live = await asyncio.wait_for(
                    temporal.query(train_id(project, env), TRAIN_STATUS_QUERY), timeout=2.0
                )
            except Exception as exc:  # no train, or Temporal is slow: the series is missing, the API lives
                logger.debug("train without state", project=project, env=env, error=str(exc)[:80])
                continue
            if not isinstance(live, dict):
                continue
            frozen[(project, env)] = 1.0 if live.get("frozen") else 0.0
            states[(project, env)] = float(TRAIN_STATES.get(str(live.get("status", "")), -1))
    return frozen, states
