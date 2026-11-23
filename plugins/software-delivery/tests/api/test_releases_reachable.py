# SPDX-License-Identifier: Apache-2.0
"""The releases list reads one page from SQL (ADR 0206): every release once, the last one reachable.

It was one of `apps/api/tests/test_in_memory_pages_are_gone.py`'s seven lists; the releases route is the
`software-delivery` plugin's since round D of ADR 0181 (S83-05c), so its test runs here, with the plugin.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from httpx import AsyncClient
from tranibor_core.domain import utcnow

API = "/api/v1"
ROWS = 1030


def _instants(count: int, *, newest: datetime | None = None) -> list[datetime]:
    """`count` instants, newest first, every third one repeated: rows born in the same instant."""
    top = (newest or utcnow()) - timedelta(minutes=5)
    return [top - timedelta(seconds=index // 3) for index in range(count)]


async def _walk(client: AsyncClient, url: str, key: str = "id", **params: Any) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        query = {"limit": 200, **params, **({"cursor": cursor} if cursor else {})}
        response = await client.get(url, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        seen += [item[key] for item in body["items"]]
        assert body["meta"]["next_cursor"] not in seen, "the cursor is opaque, not the last row's id"
        if not body["meta"]["has_more"]:
            assert body["meta"]["next_cursor"] is None
            return seen
        cursor = body["meta"]["next_cursor"]


def _once(seen: list[str], expected: set[str]) -> None:
    assert len(seen) == len(set(seen)), "a row came twice"
    assert set(seen) == expected


async def test_the_releases_are_reachable(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api.db.models import Release
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        rows = [
            Release(
                project_id=project["id"],
                env="prod" if i % 2 else "staging",
                batch_no=i,
                status="verified",
                items=[],
                created_at=ts,
            )
            for i, ts in enumerate(_instants(ROWS))
        ]
        session.add_all(rows)
        await session.flush()
        ids = {row.id for row in rows}
        prod = {row.id for row in rows if row.env == "prod"}
    url = f"{API}/projects/{project['id']}/releases"
    _once(await _walk(client, url), ids)
    _once(await _walk(client, url, env="prod"), prod)
