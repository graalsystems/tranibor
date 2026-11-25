# SPDX-License-Identifier: Apache-2.0
"""The installer re-checks a server against the profile it implements, as installed (ADR 0224 §2, S86-02).

`plugin validate` proved the server against the files it was given; the organisation holds one installed
version of the plugin that declares the profile, and that is what counts at install."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import AsyncClient

from .agent_scopes_kit import add_member, as_user

ORG = "/api/v1/orgs/northwind"
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "profiles"
SECOND = "second-admin@northwind.dev"


def _edit(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _copy(root: Path, name: str, version: str) -> Path:
    target = root / name / version
    shutil.copytree(FIXTURES / name, target)
    _edit(target / "plugin.yaml", lambda d: d["metadata"].update(version=version))
    return target


@pytest.fixture
def offline(tmp_path: Path) -> Iterator[Path]:
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    _copy(root, "notes", "1.0.0")
    _copy(root, "shelf", "1.0.0")
    # A shelf whose list_notes is a write: it breaks the profile on one rule.
    broken = _copy(root, "shelf", "1.1.0")
    _edit(
        broken / "servers" / "shelf.yaml",
        lambda d: next(op for op in d["operations"] if op["name"] == "list_notes").update(access="write"),
    )
    # A notes whose profile caps `limit` at 100: a server that serves 500 does not keep it.
    stricter = _copy(root, "notes", "1.1.0")
    _edit(
        stricter / "profiles" / "notes.yaml",
        lambda d: d["tools"][1]["input"]["properties"]["limit"].update(maximum=1000),
    )
    use_plugin_supply(OfflineDirectory(root))
    yield root
    use_plugin_supply(None)


async def _notes(client: AsyncClient, version: str) -> None:
    installed = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "notes", "version": version}
    )
    assert installed.status_code == 201, installed.text  # a plugin without a server: one administrator


async def _ask_shelf(client: AsyncClient, version: str) -> Any:
    return await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "shelf", "version": version}
    )


async def _rows() -> int:
    from sqlalchemy import func, select
    from tranibor_api.db.models import OrgPlugin
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return int((await session.execute(select(func.count()).select_from(OrgPlugin))).scalar_one())


async def test_a_server_that_keeps_the_installed_profile_is_asked_for_approval(
    client: AsyncClient, admin: str, offline: Path
) -> None:
    await _notes(client, "1.0.0")
    asked = await _ask_shelf(client, "1.0.0")
    assert asked.status_code == 202, asked.text  # it has a server: a second administrator approves
    await add_member(client, SECOND, "org_admin")
    await as_user(client, SECOND)
    approved = await client.post(f"{ORG}/plugin-approvals/{asked.json()['approval_id']}/approve")
    assert approved.status_code == 200, approved.text


async def test_a_server_that_breaks_a_rule_is_refused_and_nothing_is_written(
    client: AsyncClient, admin: str, offline: Path
) -> None:
    await _notes(client, "1.0.0")
    before = await _rows()
    refused = await _ask_shelf(client, "1.1.0")
    assert refused.status_code == 422, refused.text
    assert "`list_notes` of `notes/notes@1` is a read of the profile, declared `write`" in refused.text
    assert await _rows() == before


async def test_the_check_is_against_the_installed_profile_not_the_files_it_was_validated_with(
    client: AsyncClient, admin: str, offline: Path
) -> None:
    # `shelf` 1.0.0 conforms to notes 1.0.0 (the test above); the organisation has notes 1.1.0, whose profile
    # lets `limit` go to 1000, and a server that stops at 500 does not accept what that profile accepts.
    await _notes(client, "1.1.0")
    refused = await _ask_shelf(client, "1.0.0")
    assert refused.status_code == 422, refused.text
    assert "is 500 for the server but 1000 for the profile" in refused.text


async def test_a_plugin_that_requires_another_must_find_it_installed(
    client: AsyncClient, admin: str, offline: Path
) -> None:
    refused = await _ask_shelf(client, "1.0.0")
    assert (
        refused.status_code == 422 and "requires the plugin `notes`, which is not installed" in refused.text
    )
