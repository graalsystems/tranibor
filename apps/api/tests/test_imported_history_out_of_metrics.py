"""An imported demonstration history counts in no Prometheus gauge, and the CSV export says what it holds.

ADR 0216 kept an imported row out of budgets and agent metrics, and showed it apart in the cost reports
(`imported_usd`); its "Not done" left the gauges (`metrics.py`: run counts, start delay, cost by project,
today's cost, lead time, first-pass rate) and the CSV export counting it as measured. A Grafana panel of a
demonstration instance showed runs and spend nothing measured.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from .test_demo_history_import import DOCUMENT, URL, demo  # noqa: F401 - `demo` is a fixture

pytestmark = pytest.mark.usefixtures("demo")


def _recent() -> tuple[dict[str, Any], Any]:
    """The test document, moved to the last hour: inside every gauge's window (today, 30 and 90 days)."""
    from tranibor_core import utcnow

    now = utcnow().replace(microsecond=0)

    def at(minutes_ago: int) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")

    document = copy.deepcopy(DOCUMENT)
    item = document["work_items"][0]
    item["created_at"] = at(50)
    item["history"] = [
        {"state": "inbox", "at": at(50)},
        {"state": "in_progress", "at": at(40), "by": "admin@northwind.dev"},
        {"state": "done", "at": at(5)},
    ]
    item["runs"][0].update(started_at=at(39), duration_s=600)
    item["runs"][1].update(started_at=at(20), duration_s=120)
    item["decisions"][0]["at"] = at(41)
    document["work_items"][1]["created_at"] = at(30)
    return document, now - timedelta(minutes=50)


async def test_no_gauge_counts_an_imported_history(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api import metrics
    from tranibor_api.db.models import CostLedger
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow

    document, earliest = _recent()
    answer = await client.post(URL.format(id=project["id"]), json=document)
    assert answer.status_code == 200, answer.text
    assert answer.json()["cost_usd"] > 0

    snapshot = await metrics.refresh()
    slug = project["slug"]

    assert snapshot.runs_total == {}, "imported runs counted as runs"
    assert snapshot.runs_active == 0.0
    assert snapshot.run_start == metrics._buckets([], metrics.START_SECONDS), "imported runs' start delay"
    assert not [key for key in snapshot.cost if key[0] == slug], "imported cost counted as spent"
    if earliest.date() == utcnow().date():  # the document's hour did not straddle midnight
        assert snapshot.daily_cost.get(slug, 0.0) == 0.0, "imported cost counted in today's spend"
    assert snapshot.lead_time == metrics._buckets([], metrics.DELIVERY_SECONDS), "imported lead time"
    assert slug not in snapshot.first_pass, "an imported work item counted in the first-pass rate"

    # The same gauges still count what was measured: a live line beside the history.
    async with session_scope() as session:
        session.add(
            CostLedger(
                project_id=project["id"],
                provider="anthropic",
                model="platform/standard",
                stage_role="implement",
                cost_usd=0.5,
                cost_eur=0.46,
                fx_rate=0.92,
                ts=utcnow(),
            )
        )
    snapshot = await metrics.refresh()
    assert snapshot.cost == {(slug, "implement", "platform/standard"): pytest.approx(0.5)}
    assert snapshot.daily_cost[slug] == pytest.approx(0.5)


async def test_the_csv_export_says_what_an_import_put_in_each_line(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await client.post(URL.format(id=project["id"]), json=DOCUMENT)).status_code == 200

    response = await client.get(
        f"/api/v1/projects/{project['id']}/costs.csv", params={"group_by": "stage", "since": "2026-11-01"}
    )
    assert response.status_code == 200, response.text
    lines = [line.lstrip("﻿").split(";") for line in response.text.splitlines() if line]
    header = lines[0]
    assert header[8] == "imported_usd", "after the eight columns that were there, which keep their position"
    column = header.index("imported_usd")
    by_key = {line[0]: line for line in lines[1:] if line and line[0]}
    assert float(by_key["implement"][column]) == pytest.approx(0.7035)
    assert float(by_key["verify"][column]) == pytest.approx(0.25)
    assert float(by_key["total"][column]) == pytest.approx(0.9535)
    assert float(by_key["total"][1]) == pytest.approx(0.9535), "the cost column keeps the ledger's sum"
