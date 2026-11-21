"""The costs' CSV export. The DORA metrics are the `software-delivery` plugin's route (ADR 0181, round D):
`plugins/software-delivery/tests/api/test_dora.py`."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def test_csv_export_of_the_costs(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api.db.models import CostLedger
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow

    async with session_scope() as session:
        session.add(
            CostLedger(
                project_id=project["id"],
                provider="anthropic",
                model="platform/standard",
                stage_role="implement",
                size="M",
                tokens_in=1200,
                tokens_out=340,
                cost_usd=0.5,
                cost_eur=0.46,
                fx_rate=0.92,
                ts=utcnow(),
            )
        )

    response = await client.get(f"/api/v1/projects/{project['id']}/costs.csv", params={"group_by": "stage"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    body = response.text
    assert body.startswith("﻿"), "the BOM keeps Excel from breaking the accents"
    lines = [line for line in body.splitlines() if line]
    assert (
        lines[0].lstrip("﻿").startswith("stage;cout_usd;cout_eur")
    )  # lang: fr-ok (the CSV header is still French)
    assert any(line.startswith("implement;0.500000;0.460000;1200;340;0;1") for line in lines)
    assert any(line.startswith("total;0.500000") for line in lines)


async def test_the_csv_export_respects_the_rights(client: AsyncClient, project: dict[str, Any]) -> None:
    """An export is a read of the project: without a session, no CSV."""
    client.cookies.clear()
    response = await client.get(f"/api/v1/projects/{project['id']}/costs.csv")
    assert response.status_code == 401
