"""A plugin's server run in the API's process, on its fake, for a demonstration (ADR 0218).

Under `TRANIBOR_DEMO_PLUGIN_SERVERS=<file>` (fakes on, `TRANIBOR_ENV` `test`), installing a plugin that
declares a fake runs its server from the plugin's own Python package: no Deployment, the broker's calls
answered by that package's application behind its bearer token, a seed passed through `fake_env`. The
mode is refused in `staging`, `prod` and without the fakes; a plugin without a declared fake, or whose
server the file does not name, is refused in it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from httpx import AsyncClient
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.plugin_servers import PluginServerRuntime

from .agent_scopes_kit import add_member, as_user

ORG = "/api/v1/orgs/northwind"
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
SERVERS = FIXTURES / "plugin_servers"
SEED = [{"number": "INC0099999", "short_description": "seeded by the operator's file"}]


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    from tranibor_api.config import reset_settings_cache

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()


@pytest.fixture
async def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeKubernetesApi]:
    """The offline source serves `demo-itsm` and `hello`; the operator's file names the package of the
    first one's server and a seed; the Kubernetes API is an in-memory one that must stay empty."""
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.services.plugin_servers import (
        stop_in_process_servers,
        use_plugin_runtime,
        use_plugin_transport,
    )
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    shutil.copytree(FIXTURES / "plugins" / "demo-itsm", root / "demo-itsm" / "1.0.0")
    shutil.copytree(FIXTURES / "plugins" / "hello", root / "hello" / "1.0.0")
    use_plugin_supply(OfflineDirectory(root))
    kube = FakeKubernetesApi()
    use_plugin_runtime(PluginServerRuntime(kube, "choregos-plugins"))
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    registry = tmp_path / "servers.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "servers": [
                    {
                        "plugin": "demo-itsm",
                        "server": "demo-itsm",
                        "path": str(SERVERS),
                        "app": "demo_itsm_server:build_app",
                        "settings": "demo_itsm_server:settings_from_env",
                        "fake_env": {"DEMO_ITSM_SEED": str(tmp_path / "seed.json")},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _set(monkeypatch, TRANIBOR_DEMO_PLUGIN_SERVERS=str(registry))
    yield kube
    await stop_in_process_servers()
    use_plugin_runtime(None)
    use_plugin_transport(None)
    use_plugin_supply(None)
    monkeypatch.delenv("TRANIBOR_DEMO_PLUGIN_SERVERS")
    reset_settings_cache()


async def _install(client: AsyncClient, name: str = "demo-itsm") -> httpx.Response:
    asked = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": name, "version": "1.0.0"}
    )
    if asked.status_code != 202:
        return asked
    await add_member(client, "second-admin@northwind.dev", "org_admin")
    await as_user(client, "second-admin@northwind.dev")
    approved = await client.post(f"{ORG}/plugin-approvals/{asked.json()['approval_id']}/approve")
    await as_user(client, "admin@northwind.dev")
    return approved


async def _run_tools(client: AsyncClient, project: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from tranibor_api.db.models import Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.security import mint_run_token

    spec = {"mcp_servers": [{"connector": "demo-itsm", "tools": ["*"]}]}
    agent = await client.post(
        f"{ORG}/agents", json={"slug": "triage", "display_name": "Triage", "spec": spec}
    )
    assert agent.status_code == 201, agent.text
    async with session_scope() as session:
        item = WorkItem(project_id=project["id"], tracker_key="northwind/t#1", title="T", state="ready")
        session.add(item)
        await session.flush()
        session.add(
            Run(id="run-triage-1", work_item_id=item.id, project_id=project["id"], stage_role="triage",
                transition_id="t", status="running", agent_slug="triage", agent_version=1)
        )  # fmt: skip
    token = mint_run_token(
        "run-triage-1", project_slug="billing-api", work_item_key="northwind/t#1", ttl_minutes=30
    )
    return "/api/v1/internal/runs/run-triage-1/tools", {"Authorization": f"Bearer {token}"}


async def test_a_plugin_server_is_run_in_process_and_answers_through_the_broker(
    client: AsyncClient, admin: str, project: dict[str, Any], demo: FakeKubernetesApi
) -> None:
    installed = await _install(client)
    assert installed.status_code == 200, installed.text
    listed = (await client.get(f"{ORG}/connectors/demo-itsm")).json()
    assert listed["status"] == "deployed"
    assert {op["name"]: op["policy"] for op in listed["operations"]} == {
        "search_records": "allowed",
        "add_work_note": "approval",
    }
    for resource in ("deployments", "services", "secrets", "networkpolicies"):
        assert not demo.store(resource), f"no {resource}: the server runs in the API's process"

    found = await client.post(f"{ORG}/connectors/demo-itsm/discover")
    assert found.status_code == 200, found.text

    base, headers = await _run_tools(client, project)
    tools = {t["name"] for t in (await client.get(base, headers=headers)).json()["tools"]}
    assert "demo-itsm__search_records" in tools
    read = await client.post(f"{base}/demo-itsm__search_records", headers=headers, json={"table": "incident"})
    assert read.json()["status_code"] == 200, read.text
    answer = read.json()["result"]["structuredContent"]
    assert answer["records"] == SEED, "the package's fake answered, on the seed the file passed through"

    write = await client.post(f"{base}/demo-itsm__add_work_note", headers=headers, json={"text": "x"})
    assert write.json()["status_code"] == 202, "a write still waits for a person: the policy is the product's"
    again = await client.post(f"{base}/demo-itsm__search_records", headers=headers, json={})
    assert again.json()["result"]["structuredContent"]["notes"] == [], "nothing reached the server"


async def test_the_server_is_started_again_when_the_process_forgot_it(
    client: AsyncClient, admin: str, project: dict[str, Any], demo: FakeKubernetesApi
) -> None:
    from tranibor_api.services.plugin_servers import stop_in_process_servers

    assert (await _install(client)).status_code == 200
    await stop_in_process_servers()  # what a restart of the API does to the process's memory
    base, headers = await _run_tools(client, project)
    read = await client.post(f"{base}/demo-itsm__search_records", headers=headers, json={})
    assert read.json()["status_code"] == 200, read.text
    assert read.json()["result"]["structuredContent"]["records"] == SEED


async def test_uninstall_stops_the_server(client: AsyncClient, admin: str, demo: FakeKubernetesApi) -> None:
    from tranibor_api.services import plugin_servers

    assert (await _install(client)).status_code == 200
    assert plugin_servers.in_process_servers()._running
    gone = await client.delete(f"{ORG}/plugins/demo-itsm")
    assert gone.status_code in {200, 204}, gone.text
    assert not plugin_servers.in_process_servers()._running


@pytest.mark.parametrize(
    ("environment", "fakes", "needle"),
    [
        ("prod", True, "TRANIBOR_ENV=prod"),
        ("staging", True, "TRANIBOR_ENV=staging"),
        ("dev", False, "TRANIBOR_FAKES is off"),
    ],
)
def test_the_mode_is_refused_outside_a_demonstration(
    demo: FakeKubernetesApi, environment: str, fakes: bool, needle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install's check, with the settings of a deployment (a real prod API would refuse earlier, on
    other grounds: here only this guard is asked), and the adapter's own refusal."""
    from types import SimpleNamespace

    from tranibor_adapters.fakes.in_process_plugin_servers import load_registry
    from tranibor_api.errors import ApiError
    from tranibor_api.services import plugin_servers
    from tranibor_core.plugins import load_directory
    from tranibor_core.production import DemoPluginServersRefusedError

    plugin = load_directory(FIXTURES / "plugins" / "demo-itsm")
    settings: Any = SimpleNamespace(env=environment, fakes=fakes, demo_plugin_servers="x")
    with pytest.raises(ApiError) as refused:
        plugin_servers._check_in_process(plugin, settings)
    assert refused.value.status_code == 422 and needle in refused.value.detail
    assert "in_process" not in plugin_servers._OVERRIDES, "nothing was imported or started"

    monkeypatch.setenv("TRANIBOR_ENV", environment)
    monkeypatch.setenv("TRANIBOR_FAKES", "1" if fakes else "0")
    with pytest.raises(DemoPluginServersRefusedError, match="refused"):  # whoever calls it
        load_registry("unread.yaml")


async def test_a_plugin_without_a_declared_fake_is_refused_in_the_mode(
    client: AsyncClient, admin: str, demo: FakeKubernetesApi
) -> None:
    refused = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "hello", "version": "1.0.0"}
    )
    assert refused.status_code == 422, refused.text
    assert "declares no `fake`" in refused.text
    assert not demo.store("deployments")


async def test_a_server_the_file_does_not_name_is_refused(
    client: AsyncClient, admin: str, demo: FakeKubernetesApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.services.plugin_servers import stop_in_process_servers

    await stop_in_process_servers()
    other = tmp_path / "other.yaml"
    other.write_text(
        yaml.safe_dump({"servers": [{"plugin": "x", "server": "y", "app": "demo_itsm_server:build_app"}]}),
        encoding="utf-8",
    )
    _set(monkeypatch, TRANIBOR_DEMO_PLUGIN_SERVERS=str(other))
    refused = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "demo-itsm", "version": "1.0.0"}
    )
    assert refused.status_code == 422 and "not in the TRANIBOR_DEMO_PLUGIN_SERVERS file" in refused.text


async def test_a_package_that_cannot_be_imported_fails_the_install(
    client: AsyncClient, admin: str, demo: FakeKubernetesApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.services.plugin_servers import stop_in_process_servers

    await stop_in_process_servers()
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        yaml.safe_dump(
            {"servers": [{"plugin": "demo-itsm", "server": "demo-itsm", "app": "no_such_package:build_app"}]}
        ),
        encoding="utf-8",
    )
    _set(monkeypatch, TRANIBOR_DEMO_PLUGIN_SERVERS=str(broken))
    refused = await _install(client)
    assert refused.status_code == 422, refused.text
    assert "is its package importable" in refused.text


def test_the_core_refuses_the_mode_outside_development_under_the_fakes() -> None:
    from tranibor_core.production import DemoPluginServersRefusedError, refuse_demo_plugin_servers

    refuse_demo_plugin_servers(environment="dev", fakes="1")
    refuse_demo_plugin_servers(environment="test", fakes=True)
    for environment, fakes in (("prod", "1"), ("staging", "1"), ("dev", "0"), ("dev", None), (None, "1")):
        with pytest.raises(DemoPluginServersRefusedError):
            refuse_demo_plugin_servers(environment=environment, fakes=fakes)
