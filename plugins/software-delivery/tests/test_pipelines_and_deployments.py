# SPDX-License-Identifier: Apache-2.0
"""The webhooks of the `ci` and `cd` connector types and the provisioning steps that use the `cd` connector
are the `software-delivery` plugin's (ADR 0181, round D, S83-05i).

`tektonWebhook`, `jenkinsWebhook` and `argocdWebhook` left the core's `routers/webhooks.py` for
`tranibor_software_delivery.webhooks`, served through `extensions.declare_router` under the same paths and
operation ids; `gitops.write_project_manifests` and `argocd.wait_synced` left the core's provisioning for
`tranibor_software_delivery.provisioning`, run through the worker seam's `provisioning_steps`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tranibor_adapters import AdapterSet
from tranibor_contracts import ProjectConfig
from tranibor_core import load_preset

WEBHOOKS = {"tektonWebhook", "jenkinsWebhook", "argocdWebhook"}
STEPS = {"gitops.write_project_manifests", "argocd.wait_synced"}


def _operation_ids(router: Any) -> set[str]:
    return {route.operation_id for route in router.routes if getattr(route, "operation_id", None)}


def test_the_api_serves_the_webhooks_through_the_extension_and_no_core_router_holds_them() -> None:
    from tranibor_api.extensions import declared_routers
    from tranibor_api.main import create_app
    from tranibor_api.routers import webhooks as core
    from tranibor_software_delivery.webhooks import router

    assert router in declared_routers() and _operation_ids(router) == WEBHOOKS
    assert not _operation_ids(core.router) & WEBHOOKS
    paths = create_app().openapi()["paths"]
    for name in ("tekton", "jenkins", "argocd"):
        assert f"/api/v1/webhooks/{name}" in paths


def test_the_extension_runs_the_steps_that_use_the_cd_connector() -> None:
    from tranibor_orchestrator.activities.provisioning import CORE_STEPS
    from tranibor_orchestrator.extension_parts import extension_provisioning_steps

    declared = extension_provisioning_steps()
    assert set(declared) >= STEPS and not STEPS & CORE_STEPS
    assert all(declared[name].remediation for name in STEPS)


def _bundle(adapters: Any, org: str = "small") -> Any:
    config = ProjectConfig.model_validate(
        {
            "slug": "billing",
            "org": org,
            "repo": {"url": "https://github.com/x/billing.git", "default_branch": "main"},
        }
    )
    return SimpleNamespace(
        slug="billing", org_slug=org, config=config, policy=load_preset("team"), adapters=adapters
    )


async def test_the_manifests_are_written_through_the_cd_connector() -> None:
    from tranibor_orchestrator.activities.provisioning import _execute

    written: dict[str, dict[str, str]] = {}

    async def write_files(path: str, files: dict[str, str]) -> None:
        written[path] = files

    adapters = AdapterSet.fakes()
    adapters.extra["cd"] = SimpleNamespace(write_files=write_files)
    said = await _execute("gitops.write_project_manifests", {}, _bundle(adapters), SimpleNamespace(), {})
    [(path, files)] = written.items()
    assert path == "projects/billing/" and said == f"{len(files)} manifest(s) written to projects/billing/"
    kinds = {d["kind"] for text in files.values() for d in yaml.safe_load_all(text) if d}
    assert {"Namespace", "ResourceQuota"} <= kinds


async def test_the_applications_are_synced_or_the_step_fails() -> None:
    from tranibor_orchestrator.activities.provisioning import _execute

    adapters = AdapterSet.fakes()
    adapters.cd.set_health("choregos-project-billing", "Healthy")
    said = await _execute("argocd.wait_synced", {}, _bundle(adapters), None, {})
    assert said == "1 application(s) synced"
    adapters.cd.set_health("billing-api", "Degraded")
    with pytest.raises(RuntimeError, match="billing-api is not synced: Degraded"):
        await _execute("argocd.wait_synced", {"apps": ["billing-api"]}, _bundle(adapters), None, {})
