# SPDX-License-Identifier: Apache-2.0
"""The DORA metrics are computed on the deployments (`getProjectDora`, the `software-delivery` plugin's route
since round D of ADR 0181). These were `apps/api/tests/test_metrics.py`'s; the first nightly
`core-without-plugins` run found them left in the core's suite."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient


async def _seed_releases(project_id: str) -> None:
    """Three production releases: two successful, one rolled back then restored."""
    from tranibor_api.db.models import Deployment, Release
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow

    now = utcnow()
    plan = [
        # (days before now, status, hours between merge and the end of the deployment)
        (20, "succeeded", 6.0),
        (10, "rolled_back", None),
        (10, "succeeded", 30.0),  # the restoration, 3 h after the rollback
    ]
    async with session_scope() as session:
        for index, (days, status, lead_hours) in enumerate(plan):
            started = now - timedelta(days=days) + timedelta(hours=index)
            ended = started + timedelta(minutes=20)
            merged = ended - timedelta(hours=lead_hours) if lead_hours else ended
            release = Release(
                project_id=project_id,
                env="prod",
                batch_no=index + 1,
                status="done" if status == "succeeded" else "rolled_back",
                items=[{"work_item_key": f"northwind/billing-api#{index}", "merged_at": merged.isoformat()}],
                started_at=started,
                ended_at=ended,
            )
            session.add(release)
            await session.flush()
            session.add(
                Deployment(
                    release_id=release.id,
                    env="prod",
                    revision=f"R-{index + 1}",
                    status=status,
                    started_at=started,
                    ended_at=ended if status != "rolled_back" else started + timedelta(hours=1),
                )
            )


async def test_dora_is_computed_on_the_deployments(client: AsyncClient, project: dict[str, Any]) -> None:
    await _seed_releases(project["id"])
    report = (await client.get(f"/api/v1/projects/{project['id']}/metrics/dora")).json()

    assert report["env"] == "prod"
    assert report["deployments"] == 2, "only the successful production releases count"
    assert report["deployment_frequency"]["value"] == pytest.approx(2 / 30, abs=0.01)
    assert report["lead_time"]["value"] == pytest.approx(18.0, abs=0.5), "median of 6 h and 30 h"
    assert report["change_failure_rate"]["value"] == pytest.approx(1 / 3, abs=0.01)
    assert report["change_failure_rate"]["level"] == "low"
    assert report["time_to_restore"]["sample"] == 1
    assert report["time_to_restore"]["value"] is not None


async def test_dora_without_a_deployment_rates_nothing(client: AsyncClient, project: dict[str, Any]) -> None:
    """No data allows no verdict: `unknown`, not `low`."""
    report = (await client.get(f"/api/v1/projects/{project['id']}/metrics/dora")).json()
    assert report["deployments"] == 0
    for metric in ("deployment_frequency", "lead_time", "change_failure_rate", "time_to_restore"):
        assert report[metric]["level"] == "unknown", metric
        assert report[metric]["sample"] == 0


async def test_dora_filters_by_environment(client: AsyncClient, project: dict[str, Any]) -> None:
    await _seed_releases(project["id"])
    staging = (
        await client.get(f"/api/v1/projects/{project['id']}/metrics/dora", params={"env": "staging"})
    ).json()
    assert staging["deployments"] == 0
