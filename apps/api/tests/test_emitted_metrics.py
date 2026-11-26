"""The metrics exist, and the dashboards only cite what exists.

Before 2026-09-24: a ServiceMonitor, four alerts and six dashboards read sixteen `choregos_*`
series that no process emitted. Six empty dashboards, delivered as if they worked. The first test
prevents that from coming back; the second checks that `/metrics` carries the series, computed
from the database.

Since 0.30.0 (ADR 0226, R9b) the series are `tranibor_*`, and each is served under its former
`choregos_*` name too, with the same samples, until 0.32.0: an operator's dashboards and rules read the
former names. The chart's own dashboards and alerts read the new ones.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

from httpx import AsyncClient

ROOT = pathlib.Path(__file__).resolve().parents[3]
DASHBOARDS = ROOT / "charts" / "tranibor" / "dashboards"
HELPERS = ROOT / "charts" / "tranibor" / "templates" / "_helpers.tpl"
SERIES = re.compile(r"\b(?:tranibor|choregos)_[a-z_]+")
#: The templates that hold the chart's alert rules (`monitoring.yaml` includes them).
ALERT_RULES = re.compile(r'^"tranibor\.(?:alertRules|[a-zA-Z]+AlertRules)"')
SUFFIXES = ("_bucket", "_count", "_sum")


def _base(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def test_the_dashboards_and_the_alerts_only_cite_emitted_metrics() -> None:
    from tranibor_api.metrics import NAMES, PLUGIN_SERIES

    # A plugin's series (the release train's, ADR 0181 round D) is emitted where its extension is installed,
    # which the published images do: the core knows it by name.
    NAMES = NAMES | set(PLUGIN_SERIES)  # noqa: N806 - the inventory, plugins' series included
    cited: dict[str, set[str]] = {}
    for file in sorted(DASHBOARDS.glob("*.json")):
        queries = [
            t["expr"]
            for panel in json.loads(file.read_text()).get("panels", [])
            for t in panel.get("targets", [])
        ]
        cited[file.name] = {_base(m) for query in queries for m in SERIES.findall(query)}
    rules = [chunk for chunk in HELPERS.read_text().split("{{- define ") if ALERT_RULES.match(chunk)]
    assert len(rules) == 3, "the alert rules' templates moved: follow them"
    cited["alert rules"] = {_base(m) for rule in rules for m in SERIES.findall(rule)}
    absent = {f: sorted(names - NAMES) for f, names in cited.items() if names - NAMES}
    assert not absent, f"metrics cited but never emitted: {absent}"
    assert sum(len(n) for n in cited.values()) >= 12, "the dashboards must still read series"
    former = {f: sorted(n for n in names if n.startswith("choregos_")) for f, names in cited.items()}
    assert not any(former.values()), f"the chart reads the new series (ADR 0226, R9b): {former}"


async def test_metrics_exposes_the_series_from_the_database(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import CostLedger, Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.metrics import emitted_names, refresh
    from tranibor_core import utcnow

    async with session_scope() as session:
        item = WorkItem(
            project_id=project["id"], tracker_key="northwind/billing-api#9", title="T", state="ready"
        )
        session.add(item)
        await session.flush()
        session.add(
            Run(
                id="northwind-billing-api-9-t-implement-1",
                work_item_id=item.id,
                project_id=project["id"],
                transition_id="t-implement",
                stage_role="implement",
                attempt=1,
                actor="agent",
                backend="claude-code",
                model="platform/standard",
                status="running",
                started_at=utcnow(),
                stage_input={},
            )
        )
        session.add(
            CostLedger(
                project_id=project["id"],
                work_item_id=item.id,
                run_id=None,
                provider="anthropic",
                model="platform/standard",
                backend="claude-code",
                stage_role="implement",
                size="M",
                tokens_in=10,
                tokens_out=5,
                tokens_cached=0,
                cost_usd=0.5,
                cost_eur=0.46,
                fx_rate=0.92,
                ts=utcnow(),
                kind="model",
            )
        )
    await refresh()
    response = await client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    for name in emitted_names():
        assert name in body, f"series missing from /metrics: {name}"
    for prefix in ("tranibor", "choregos"):  # the new name, and the former for the window (R9b)
        assert f"{prefix}_runs_active 1.0" in body
        labels = 'model="platform/standard",project="billing-api",stage="implement"'
        assert f"{prefix}_cost_usd_total{{{labels}}} 0.5" in body
        assert f'{prefix}_project_daily_cost_usd{{project="billing-api"}} 0.5' in body
    assert "http_requests_total{" in body
    assert "choregos_http_requests_total" not in body, "a series the platform does not prefix has no twin"


async def test_every_series_is_served_under_its_former_name_with_the_same_samples(
    client: AsyncClient,
) -> None:
    """ADR 0226 (R9b): an operator's dashboards, recording rules and alerts read `choregos_*`. The twin
    is the same family under the former name, so the two can never disagree."""
    from prometheus_client.parser import text_string_to_metric_families
    from tranibor_api.metric_series import CURRENT_NAMES, former_name
    from tranibor_api.metrics import refresh

    await refresh()
    response = await client.get("/metrics")
    families = {f.name: f for f in text_string_to_metric_families(response.text)}
    platform = {name for name in families if name.startswith("tranibor_")}
    assert platform, "the platform's series are served"
    for name in sorted(platform):
        twin = former_name(name)
        assert twin is not None and twin in families, f"{name} has no former twin"
        renamed = [s._replace(name=former_name(s.name) or s.name) for s in families[name].samples]
        assert families[twin].samples == renamed, name
        assert families[twin].type == families[name].type and families[twin].documentation
    # Every inventoried series, under both names (a counter's family drops its `_total` in the parser).
    for name in CURRENT_NAMES:
        if name.startswith("tranibor_"):
            assert name in response.text and str(former_name(name)) in response.text, name
    assert not [n for n in families if n.startswith("choregos_") and f"tranibor_{n[9:]}" not in families]


async def test_every_response_carries_a_request_identifier(client: AsyncClient) -> None:
    """The API's log and the front end's are linked by this identifier."""
    without = await client.get("/healthz")
    assert len(without.headers["x-request-id"]) >= 12
    with_one = await client.get("/healthz", headers={"X-Request-Id": "req-from-the-front-42"})
    assert with_one.headers["x-request-id"] == "req-from-the-front-42"
