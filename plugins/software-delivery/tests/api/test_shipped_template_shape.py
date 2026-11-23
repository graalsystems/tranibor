# SPDX-License-Identifier: Apache-2.0
"""The shipped `github-aca` template keeps its shape (S16-07), moved from
`apps/api/tests/test_multi_workflow_template.py` (ADR 0181, round D): its `default-simple` names the
plugin's gates, so a project is born of it where the extension is installed. The singular
`defaults.workflow` rule is the core suite's (`test_a_template_s_single_workflow_keeps_its_shape`)."""

from __future__ import annotations

from httpx import AsyncClient


async def test_the_shipped_templates_keep_their_shape(client: AsyncClient, admin: str) -> None:
    """`defaults.workflow`, singular: the shape of the templates shipped so far."""
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={
            "slug": "on-azure",
            "name": "on-azure",
            "template_ref": "github-aca@1.0.0",
            "config": {"slug": "on-azure", "org": "northwind"},
        },
    )
    assert created.status_code == 201, created.text
    active = (await client.get(f"/api/v1/projects/{created.json()['id']}/workflows")).json()["items"]
    assert [w["name"] for w in active] == ["default-simple"]
