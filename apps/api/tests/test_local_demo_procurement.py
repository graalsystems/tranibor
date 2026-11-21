"""The procurement project of the local install (`tranibor_api.local_demo`, S85-11, ADR 0202).

The plugin itself is not in this repository's tests: `plugins/offline/` ships its signed archive, and what
that archive holds is proven where it is built. A stand-in `procurement` plugin, with the same template and
workflow names and the same `metadata.attachments`, goes through the same installer, so that what is proven
here is the seed: the plugin installed, the project born of its template, the groups, the three offers
handed to a work item that is NOT started, and a second run that changes nothing (ADR 0008).
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from tranibor_adapters.fakes import FakeObjectStore
from tranibor_api.config import Settings
from tranibor_api.local_demo import (
    PROCUREMENT_GROUPS,
    PROCUREMENT_PROJECT,
    PROCUREMENT_SAMPLES,
    ProcurementDemoError,
    procurement_samples_dir,
    seed_local_demo,
    seed_procurement_demo,
)

from .conftest import login

EMAIL = "admin@choregos.local"
FIXTURES = pathlib.Path(__file__).resolve().parents[3] / "plugins" / "procurement" / "tests" / "fixtures"

PLUGIN = """\
apiVersion: choregos/v1
kind: Plugin
metadata:
  name: procurement
  version: 0.2.0
  publisher: GRAAL.SYSTEMS SAS
  license: Apache-2.0
  description: A stand-in for the procurement plugin, with its template and its workflow's names.
  homepage: https://plugins.example.test/procurement
  core: ">=0.20.0"
requires:
  plugins: []
  connectors: {}
contents:
  templates: [templates/procurement-sourcing]
"""

TEMPLATE = """\
apiVersion: choregos/v1
kind: Template
metadata:
  name: procurement-sourcing
  version: 0.1.0
  display: Procurement
  description: A consultation is a work item whose offers are handed as files.
requires:
  connectors:
    tracker: internal
defaults:
  workflows: [./workflows/offer-comparison.yaml]
  default_workflow: offer-comparison
  policy: preset:regulated
inputs: []
steps: []
"""

WORKFLOW = """\
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: offer-comparison
  version: 1
  description: Offers handed as files, then a buyer decides.
  attachments: { accept: [pdf, docx, xlsx, csv], min: 2, max: 20 }
  inputs:
    type: object
    required: [currency]
    properties:
      currency: { type: string, pattern: "^[A-Z]{3}$" }
initial: received
actors:
  buyer: { type: human, group: buyers, sla_hours: 48 }
states:
  received: { display: Offers received, kind: wait }
  awarded: { display: Awarded, terminal: true }
transitions:
  - { id: t-award, from: received, to: awarded, by: buyer }
"""


@pytest.fixture
def offline(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    from tranibor_api.object_store import use_object_store
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    target = root / "procurement" / "0.2.0"
    (target / "templates" / "procurement-sourcing" / "workflows").mkdir(parents=True)
    (target / "plugin.yaml").write_text(PLUGIN, encoding="utf-8")
    (target / "templates" / "procurement-sourcing" / "manifest.yaml").write_text(TEMPLATE, encoding="utf-8")
    (target / "templates" / "procurement-sourcing" / "workflows" / "offer-comparison.yaml").write_text(
        WORKFLOW, encoding="utf-8"
    )
    use_plugin_supply(OfflineDirectory(root))
    use_object_store(FakeObjectStore())
    yield root
    use_plugin_supply(None)
    use_object_store(None)


@pytest.fixture
def empty_offline(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    (tmp_path / "nothing").mkdir()
    use_plugin_supply(OfflineDirectory(tmp_path / "nothing"))
    yield tmp_path / "nothing"
    use_plugin_supply(None)


def _settings() -> Settings:
    return Settings(bootstrap_org="demo", bootstrap_org_name="Demo", bootstrap_admins=EMAIL)


async def _the_demo_user() -> None:
    from tranibor_api.db.models import User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        session.add(User(email=EMAIL, display_name="admin"))


async def _counts() -> dict[str, int]:
    from tranibor_api.db.models import (
        ArtifactRecord,
        Connector,
        OrgGroup,
        OrgGroupMember,
        OrgPlugin,
        Project,
        WorkItem,
    )
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return {
            model.__tablename__: int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
            for model in (OrgPlugin, Project, Connector, OrgGroup, OrgGroupMember, WorkItem, ArtifactRecord)
        }


async def test_the_seed_opens_the_procurement_project_with_three_offers_and_starts_nothing(
    app: Any, offline: pathlib.Path
) -> None:
    from tranibor_api.db.models import (
        ArtifactRecord,
        Organization,
        OrgGroup,
        OrgGroupMember,
        Project,
        WorkItem,
    )
    from tranibor_api.db.session import session_scope
    from tranibor_api.temporal import get_temporal

    await _the_demo_user()
    await seed_local_demo(_settings())
    first = await seed_procurement_demo(_settings(), samples=FIXTURES)
    assert first["plugins"] == ["procurement@0.2.0"]
    assert first["projects"] == [PROCUREMENT_PROJECT]
    assert first["groups"] == [slug for slug, _, _ in PROCUREMENT_GROUPS]
    assert sorted(first["members"]) == sorted(f"{slug}:{EMAIL}" for slug, _, _ in PROCUREMENT_GROUPS)
    assert len(first["work_items"]) == 1

    async with session_scope(orgs="*") as session:
        org = (await session.execute(select(Organization).where(Organization.slug == "demo"))).scalar_one()
        project = (
            await session.execute(select(Project).where(Project.slug == PROCUREMENT_PROJECT))
        ).scalar_one()
        assert project.template_ref == "plugin:procurement/procurement-sourcing"
        assert project.default_workflow == "offer-comparison"
        for slug, _, _ in PROCUREMENT_GROUPS:
            group = (
                await session.execute(
                    select(OrgGroup).where(OrgGroup.org_id == org.id, OrgGroup.slug == slug)
                )
            ).scalar_one()
            members = (
                await session.execute(select(OrgGroupMember).where(OrgGroupMember.group_id == group.id))
            ).scalars()
            assert len(list(members)) == 1
        item = (await session.execute(select(WorkItem).where(WorkItem.project_id == project.id))).scalar_one()
        assert item.state == "received"
        assert item.temporal_wf_id is None or item.temporal_wf_id not in get_temporal().started  # type: ignore[attr-defined]
        handed = list(
            (
                await session.execute(select(ArtifactRecord).where(ArtifactRecord.work_item_id == item.id))
            ).scalars()
        )
        assert sorted(a.name for a in handed) == sorted(PROCUREMENT_SAMPLES)
        for name in PROCUREMENT_SAMPLES:
            row = next(a for a in handed if a.name == name)
            assert row.size == (FIXTURES / name).stat().st_size


async def test_running_the_seed_twice_changes_nothing(app: Any, offline: pathlib.Path) -> None:
    await _the_demo_user()
    await seed_local_demo(_settings())
    await seed_procurement_demo(_settings(), samples=FIXTURES)
    before = await _counts()
    assert before["org_plugins"] == 1 and before["artifacts"] >= 3

    again = await seed_procurement_demo(_settings(), samples=FIXTURES)

    assert again == {
        "plugins": [],
        "projects": [],
        "connectors": [],
        "switched": [],
        "groups": [],
        "members": [],
        "work_items": [],
    }
    assert await _counts() == before


async def test_the_person_starts_the_seeded_consultation_from_the_console(
    app: Any, client: AsyncClient, offline: pathlib.Path
) -> None:
    from tranibor_api.temporal import get_temporal

    await _the_demo_user()
    await seed_local_demo(_settings())
    report = await seed_procurement_demo(_settings(), samples=FIXTURES)
    await login(client, "admin@northwind.dev")  # a dev admin of every organisation of the bench
    page = await client.get(f"/api/v1/projects/demo:{PROCUREMENT_PROJECT}/work-items")
    assert page.status_code == 200, page.text
    [item] = [i for i in page.json()["items"] if i["tracker_key"] == report["work_items"][0]]
    assert item["state"] == "received"
    started = await client.post(
        f"/api/v1/work-items/{item['id']}/actions", json={"action": "mark_agent_ready"}
    )
    assert started.status_code == 202, started.text
    assert any(item["tracker_key"] in wf for wf in get_temporal().started), "the interpreter started"  # type: ignore[attr-defined]


async def test_a_plugin_the_image_does_not_carry_stops_the_seed_loudly(
    app: Any, empty_offline: pathlib.Path
) -> None:
    await _the_demo_user()
    await seed_local_demo(_settings())
    with pytest.raises(ProcurementDemoError, match="holds no `procurement`"):
        await seed_procurement_demo(_settings(), samples=FIXTURES)
    assert (await _counts())["projects"] == 1, "no half a project is left behind"


async def test_a_missing_sample_stops_the_seed_before_it_writes_anything(
    app: Any, offline: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    await _the_demo_user()
    await seed_local_demo(_settings())
    before = await _counts()
    with pytest.raises(ProcurementDemoError, match=r"alderbrook-offer\.pdf"):
        await seed_procurement_demo(_settings(), samples=tmp_path)
    assert await _counts() == before


def test_the_samples_are_the_plugins_own_text_layer_fixtures() -> None:
    """Three offers a reader can read: not the scan, the encrypted file or the injection fixture."""
    for name in PROCUREMENT_SAMPLES:
        assert (FIXTURES / name).is_file(), name
    assert {"scan-only.pdf", "locked-offer.pdf", "corvane-injected.pdf"}.isdisjoint(PROCUREMENT_SAMPLES)


def test_the_samples_directory_is_the_environments_then_the_image_then_the_checkout(
    tmp_path: pathlib.Path,
) -> None:
    assert procurement_samples_dir({"TRANIBOR_SAMPLES_DIR": str(tmp_path)}) == tmp_path / "procurement"
    assert procurement_samples_dir({}).is_dir(), "a source checkout finds the fixtures without any setting"
