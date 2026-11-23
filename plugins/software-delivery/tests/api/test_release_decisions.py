# SPDX-License-Identifier: Apache-2.0
"""A release's approval is a decision like the others: an outpost's signature, an API token or a stale session
does not take it (S67-06, S67-15, ADR 0147 §3, ADR 0154 §7).

The release routes are the `software-delivery` plugin's since round D of ADR 0181 (S83-05c): their halves of
`apps/api/tests/test_decision_names_this_item_request.py`, `test_decisions_need_a_session.py` and
`test_gesture_checks.py` run here, with the plugin.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

EXTENSION = """
import time

from tranibor_api.extensions import GestureRefused, declare_gesture_check

STATE = {"freshness_s": None, "seen": []}


def fresh_authentication(session, request):
    STATE["seen"].append(request)
    freshness = STATE["freshness_s"]
    if freshness is None:
        return
    when = request.principal.authenticated_at
    if when is None or time.time() - when > freshness:
        raise GestureRefused("approving takes an authentication less than 5 minutes old", "reauth")


def plug_in():
    declare_gesture_check("release.approve", "freshness", fresh_authentication)
"""


@pytest.fixture
def extension(tmp_path: pathlib.Path) -> Iterator[dict[str, Any]]:
    """An INSTALLED extension (a real module with its `.dist-info`) that checks the approval's freshness."""
    from tranibor_adapters import load_extensions
    from tranibor_api.extensions import reset
    from tranibor_software_delivery.extension import register

    root = tmp_path / "site"
    root.mkdir()
    (root / "extension_gestures.py").write_text(EXTENSION, encoding="utf-8")
    info = root / "extension_gestures-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: extension_gestures\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        "[choregos.plugins]\nextension_gestures = extension_gestures:plug_in\n", encoding="utf-8"
    )
    sys.path.insert(0, str(root))
    try:
        assert "extension_gestures" in load_extensions()
        yield sys.modules["extension_gestures"].STATE
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("extension_gestures", None)
        reset()
        register()


async def _token_client(app: Any, client: AsyncClient) -> AsyncClient:
    """A client that holds an API token of the signed-in administrator and no session."""
    token = (await client.post("/api/v1/me/tokens", json={"name": "ci", "scopes": ["*"]})).json()["token"]
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    )


async def _release(project: dict[str, Any]) -> str:
    from tranibor_api.db.models import Release
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        release = Release(project_id=project["id"], env="prod", status="awaiting_approval", items=[])
        session.add(release)
        await session.flush()
        return str(release.id)


async def _approved_by(release_id: str) -> str | None:
    from tranibor_api.db.models import Release
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = await session.get(Release, release_id)
        assert row is not None
        return row.approved_by


async def test_an_outposts_signature_does_not_approve_a_release(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    """Refused before any session is read, whoever the session is (here the organisation's administrator)."""
    from tranibor_contracts import SIGNATURE_HEADER

    release_id = await _release(project)
    for path in ("/api/v1/releases/r1/approve", f"/api/v1/releases/{release_id}/approve"):
        refused = await client.post(path, json={"note": ""}, headers={SIGNATURE_HEADER: "0" * 64})
        assert refused.status_code == 403, f"{path}: {refused.text}"
        assert "outposts' door only" in refused.json()["detail"], path
    assert await _approved_by(release_id) is None


async def test_a_token_does_not_approve_a_release(
    client: AsyncClient, app: Any, project: dict[str, Any]
) -> None:
    from tranibor_api.decision_session import CODE

    release_id = await _release(project)
    async with await _token_client(app, client) as machine:
        for path in ("/api/v1/releases/r1/approve", f"/api/v1/releases/{release_id}/approve"):
            refused = await machine.post(path, json={"note": ""})
            assert refused.status_code == 403 and refused.json()["code"] == CODE, refused.text
    assert await _approved_by(release_id) is None


async def test_a_release_approval_goes_through_the_same_check(
    client: AsyncClient, project: dict[str, Any], extension: dict[str, Any]
) -> None:
    from tranibor_api.db.models import Release
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        release = Release(project_id=project["id"], env="prod", status="awaiting_approval", items=[])
        session.add(release)
        await session.flush()
        release_id = str(release.id)
    extension["freshness_s"] = -1  # no session is fresh enough any more
    refused = await client.post(f"/api/v1/releases/{release_id}/approve", json={"note": "go"})
    assert refused.status_code == 401, refused.text
    assert extension["seen"][-1].target == {"project": "billing-api", "release": release_id, "env": "prod"}
    async with session_scope(orgs="*") as session:
        assert (await session.get(Release, release_id)).approved_by is None  # type: ignore[union-attr]
