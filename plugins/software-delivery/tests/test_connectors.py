# SPDX-License-Identifier: Apache-2.0
"""The `ci` and `cd` connector types are the `software-delivery` plugin's extension's (ADR 0181, round D).

The extension registers them under the kinds and type names stored connectors carry, with their fakes,
and declares the `ci` and `cd` slots of every project's adapter set; the core registers none of them.
These are the parts of the adapters' suite that read the `ci` and `cd` types beside the core's (the fakes'
protocols, Argo CD's HTTP contract, the Azure DevOps client, self-managed GitLab, the deployment's fields).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import Response
from tranibor_adapters import AdapterSet, available, build, connector_types, registry, validate_config
from tranibor_adapters.azure_devops import AzureDevOpsClient, azure_egress
from tranibor_adapters.errors import ConfigurationError
from tranibor_adapters.fakes.hr import extension_desks
from tranibor_adapters.fakes.server import DemoApplication
from tranibor_adapters.tracker.gitlab_instance import build_client
from tranibor_contracts import CONNECTOR_KINDS
from tranibor_core.domain import Change
from tranibor_software_delivery.connectors import (
    FACTORIES,
    CdAdapter,
    CiAdapter,
    cd_of,
    ci_of,
    register_connectors,
)
from tranibor_software_delivery.connectors.ci.azure_pipelines import AzurePipelinesCi
from tranibor_software_delivery.connectors.ci.gitlab import GitLabCi
from tranibor_software_delivery.connectors.fakes import FakeCd, FakeCi, FakeDemoCd
from tranibor_software_delivery.names import NAME

TYPES = {
    ("ci", "tekton"),
    ("ci", "gitlab"),
    ("ci", "github-actions"),
    ("ci", "jenkins"),
    ("ci", "azure-pipelines"),
    ("cd", "argocd"),
    ("cd", "demo"),
}


@pytest.fixture
def no_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)


def test_the_extension_registers_the_types_and_their_fakes_under_their_stored_names() -> None:
    assert set(FACTORIES) == TYPES
    registered = {(kind, name) for kind, name, _ in connector_types() if kind in {"ci", "cd"}}
    assert registered >= TYPES
    for kind, name in TYPES:
        factory = registry._REGISTRY[(kind, name)]
        assert factory.__module__.startswith("tranibor_software_delivery."), (kind, name)
    assert {"fake"} <= set(available("ci")) & set(available("cd"))
    assert {"ci", "cd"} <= CONNECTOR_KINDS.declared_by(NAME)


def test_the_secrets_are_secret_fields_and_the_demo_environment_exposes_no_operation() -> None:
    specs = {(k, t): spec for k, t, spec in connector_types()}
    assert "token" in specs[("cd", "argocd")].secret_fields
    for pair in TYPES:
        spec = specs[pair]
        assert not set(spec.secret_fields) & set(spec.config_schema.get("properties", {})), pair
    # The demo `cd` (S21-24) exposes nothing: the train calls it, never an agent.
    assert specs[("cd", "demo")].operations == () and specs[("cd", "demo")].dev_only


def test_registering_again_changes_nothing() -> None:
    before = dict(registry.adapter_slots())
    register_connectors()
    assert registry.adapter_slots() == before


def test_every_adapter_set_gets_the_ci_and_cd_slots() -> None:
    slots = registry.adapter_slots()
    assert (slots["ci"].plugin, slots["ci"].default_type) == (NAME, "tekton")
    assert (slots["cd"].plugin, slots["cd"].default_type) == (NAME, "argocd")
    assert registry.default_type("ci") == "tekton" and registry.default_type("cd") == "argocd"
    adapters = AdapterSet.fakes()
    assert isinstance(ci_of(adapters), FakeCi) and isinstance(cd_of(adapters), FakeCd)
    assert adapters.ci is ci_of(adapters) and adapters.cd is cd_of(adapters)
    assert isinstance(adapters.ci, CiAdapter) and isinstance(adapters.cd, CdAdapter)


def test_a_slot_the_core_holds_or_another_plugin_declared_is_refused() -> None:
    with pytest.raises(ValueError, match="slot of the core's AdapterSet"):
        registry.declare_adapter_slot("tracker", plugin=NAME, default_type="x", fake=object)
    with pytest.raises(ValueError, match="already declared by the plugin `software-delivery`"):
        registry.declare_adapter_slot("ci", plugin="another", default_type="x", fake=object)


def test_a_project_without_connectors_gets_the_default_types(no_fakes: None) -> None:
    built: dict[str, str] = {}
    original = registry.build

    def recording(kind: Any, type_name: str, config: dict[str, Any] | None = None) -> Any:
        built[str(kind)] = type_name
        return object() if str(kind) in {"ci", "cd"} else original(kind, type_name, config)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, "build", recording)
        adapters = AdapterSet.from_connectors({"tracker": {"type": "internal"}, "runtime": {"type": "fake"}})
    assert (built["ci"], built["cd"]) == ("tekton", "argocd")
    assert set(adapters.extra) == {"ci", "cd"}


def test_a_tekton_connector_does_not_name_the_api_server(no_fakes: None) -> None:
    """S65-03, ADR 0172: where a pipeline's cluster is is the deployment's to say."""
    elsewhere = {"kubernetes": {"base_url": "https://k8s.elsewhere.example", "verify": False}}
    with pytest.raises(ConfigurationError, match="the deployment's to set"):
        validate_config("ci", "tekton", elsewhere)
    # Where a project's pipelines run is the project's to say.
    validate_config("ci", "tekton", {"namespace": "proj-billing-ci", "pipeline": "billing-ci"})
    with pytest.raises(ConfigurationError, match="the deployment's to set"):
        AdapterSet.from_connectors({"ci": {"type": "tekton", "config": elsewhere}})


async def test_the_fakes_promote_roll_back_and_report() -> None:
    cd = AdapterSet.fakes().cd
    ref = await cd.promote("prod", [Change(app="billing-api", tag="v1.2.3")], "R-2026.09.18-1")
    assert ref.url and ref.merged
    assert (await cd.current_revision("billing-api")) == "v1.2.3"
    assert (await cd.health("billing-api")).status == "Healthy"
    cd.fail_next_analysis()
    assert (await cd.rollout_status("billing-api")).phase == "Degraded"
    await cd.abort_rollout("billing-api")
    assert "billing-api" in cd.aborted

    ci = AdapterSet.fakes().ci
    ci.set_status("acme/billing", "sha1", "failure", logs="line1\nline2\nERROR")
    status = await ci.status_for("acme/billing", "sha1")
    assert status.state == "failure"
    assert "ERROR" in await ci.logs("acme/billing@sha1", tail=1)
    events = ci.parse_event({"ce-id": "x"}, json.dumps({"state": "failed"}).encode())
    assert events[0].type == "ci.run.failed"


def test_the_fakes_process_serves_the_demo_environment() -> None:
    """`cd: demo` with a `url` reaches the fakes' process (S21-24): the extension declares its desk."""
    assert extension_desks()["cd"] is FakeDemoCd
    assert isinstance(DemoApplication().fakes["cd"], FakeDemoCd)


def test_azure_pipelines_is_built_on_the_shared_azure_devops_client(no_fakes: None) -> None:
    good: dict[str, Any] = {"base_url": "https://dev.azure.com/northwind", "project": "Billing", "pat": "x"}
    entra = {**good, "pat": None, "auth": "entra", "tenant_id": "t", "client_id": "c", "client_secret": "s"}
    ci = build("ci", "azure-pipelines", {**good, "repository": "billing-api", "pipeline": "ci"})
    assert isinstance(ci, AzurePipelinesCi) and isinstance(ci.client, AzureDevOpsClient)
    assert (ci.client.service, ci.pipeline, ci.repository) == ("azure-pipelines", "ci", "billing-api")
    spec = {(k, t): s for k, t, s in connector_types()}[("ci", "azure-pipelines")]
    assert set(spec.secret_fields) == {"pat", "client_secret"} and spec.egress is azure_egress
    assert spec.config_schema["required"] == ["base_url", "project"]
    assert registry.account_of("ci", "azure-pipelines", entra) == "c"
    assert build("ci", "azure-pipelines", entra).client.entra is not None


async def test_gitlab_ci_calls_a_self_managed_instance_under_its_path_prefix(no_fakes: None) -> None:
    prefixed = "https://tools.corp.example:8443/gitlab"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/api/v4/version"):
            return httpx.Response(200, json={"version": "16.0.1-ee", "revision": "abc"})
        return httpx.Response(200, json={"id": 1, "path_with_namespace": "ops/billing"})

    client = build_client({"token": "glpat-x", "base_url": prefixed})
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verdict = await GitLabCi(client, "ops/billing").test()
    assert verdict["ok"] is True and verdict["version"] == "16.0.1-ee"
    assert seen and all(r.url.path.startswith("/gitlab/api/v4/") for r in seen)
    built = build("ci", "gitlab", {"base_url": prefixed + "/", "project": "ops/billing", "token": "t"})
    assert built.client.base_url == prefixed
    # The version gate holds for the CI as for the core's GitLab connectors (15.0 or later).
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"version": "9.9.9"}))
    )
    assert (await GitLabCi(client, "ops/billing").test())["ok"] is False


ARGO = "http://argocd.test"


@respx.mock
async def test_argocd_health_revision_and_unknown_application() -> None:
    from tranibor_software_delivery.connectors.cd.argocd import ArgoCdAdapter

    respx.get(f"{ARGO}/api/v1/applications/billing-api-prod").mock(
        return_value=Response(
            200,
            json={
                "status": {
                    "health": {"status": "Degraded", "message": "1/3 pods"},
                    "sync": {"revision": "abc123"},
                }
            },
        )
    )
    respx.get(f"{ARGO}/api/v1/applications/nowhere").mock(return_value=Response(404, text="not found"))
    argo = ArgoCdAdapter(base_url=ARGO, token="t")
    health = await argo.health("billing-api-prod")
    assert (health.status, health.message, health.revision) == ("Degraded", "1/3 pods", "abc123")
    assert await argo.current_revision("billing-api-prod") == "abc123"
    assert (await argo.health("nowhere")).status == "Missing"
    assert await argo.current_revision("nowhere") == "unknown"


@respx.mock
async def test_argocd_an_aborted_rollout_reads_from_its_conditions() -> None:
    from tranibor_software_delivery.connectors.cd.argocd import ArgoCdAdapter

    manifest = {
        "spec": {"strategy": {"canary": {"steps": [{"setWeight": 10}, {"pause": {}}, {"setWeight": 50}]}}},
        "status": {
            "phase": "Paused",
            "currentStepIndex": 1,
            "canary": {"weight": 10},
            "conditions": [{"reason": "RolloutAborted"}],
            "message": "SLO analysis failed",
        },
    }
    respx.get(f"{ARGO}/api/v1/applications/billing-api-prod/resource").mock(
        return_value=Response(200, json={"manifest": json.dumps(manifest)})
    )
    rollout = await ArgoCdAdapter(base_url=ARGO).rollout_status("billing-api-prod")
    assert (
        rollout.phase == "Aborted"
        and rollout.current_step == 1
        and rollout.total_steps == 3
        and rollout.canary_weight == 10
    )
    assert "SLO" in rollout.message

    abort = respx.post(f"{ARGO}/api/v1/applications/billing-api-prod/rollback").mock(
        return_value=Response(200, json={})
    )
    await ArgoCdAdapter(base_url=ARGO).abort_rollout("billing-api-prod")
    assert json.loads(abort.calls[0].request.content) == {"name": "billing-api-prod", "prune": False}


def test_github_actions_and_argocd_name_the_repository_the_github_app_pins() -> None:
    """ADR 0073: the API pins a GitHub-served connector to the installation its organisation claimed; the
    plugin's types say which key names their repository, as the core's do."""
    from tranibor_api.services.github_app import repo_of, served_by_the_app

    assert repo_of("cd", "argocd", {"gitops_repo": "o/g", "repo": "x/y"}) == "o/g"
    assert repo_of("ci", "github-actions", {"repo": "o/r"}) == "o/r"
    assert served_by_the_app("cd", "argocd") and not served_by_the_app("ci", "tekton")


def test_every_type_ships_with_its_setup_procedure() -> None:
    """ADR 0067: a type ships with its procedure, the plugin's as the core's (`tests/procedures`)."""
    from tranibor_core import procedures

    catalogue = procedures.load()
    missing = [f"{k}/{t}" for k, t in sorted(TYPES) if catalogue.for_target(f"connector:{k}/{t}") is None]
    assert missing == []
