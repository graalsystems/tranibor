"""The agent, skill, auth, project and work-item routers are English (S45-15, ADR 0098), and not one
route, operation id or persisted audit key moved while they were translated."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import ModuleType
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = "apps/api/src/tranibor_api/routers/"
RECORDED = pathlib.Path(__file__).parent / "fixtures" / "routes_before_s45_15.json"

#: The routers S45-15 owns. Not listed: the nine of S45-16 (internal, org_connectors, connectors,
#: connector_catalogue, mcp_catalogue, outbound_proxy, webhooks, runs, actions) and the routers
#: that were written in English (plugins, plugin_sources, mcp_gateway, tracker_identities). `trains` left the
#: core for the software-delivery plugin (#2117, ADR 0181): its routes are still served (see
#: `test_routes_unchanged`), but its file is no longer a router of the core.
OWNED = (
    "__init__",
    "admin",
    "agent_realtime",
    "agent_releases",
    "agent_session_pods",
    "agent_sessions",
    "agents",
    "auth",
    "budget_alerts",
    "costs",
    "egress",
    "findings",
    "github_app",
    "integrations",
    "knowledge",
    "memories",
    "memory",
    "model_catalogue",
    "models",
    "platform_identity",
    "procedures",
    "projects",
    "readiness",
    "received_events",
    "remote_credentials",
    "skill_catalogue",
    "skills",
    "templates",
    "theme",
    "triggers",
    "ui",
    "workflow_catalogue",
    "workflows",
    "workitems",
)


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_english_s45_15", ROOT / "tools" / "check_english.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass needs its module registered
    spec.loader.exec_module(module)
    return module


def _sources() -> list[str]:
    """Each owned router's file, or every module of it once it is a package."""
    sources: list[str] = []
    for name in OWNED:
        package = ROOT / SRC / name
        if package.is_dir():
            sources += sorted(f"{SRC}{name}/{path.name}" for path in package.glob("*.py"))
        else:
            sources.append(f"{SRC}{name}.py")
    return sources


def test_owned_routers_have_no_french() -> None:
    checker = _checker()
    try:
        found = checker.scan_tree(_sources())
    except RuntimeError:  # an exported tree without .git: nothing to scan
        pytest.skip("not a git checkout")
    assert [str(f) for f in found] == []


def test_every_owned_router_exists() -> None:
    # A router split into a package (routers/knowledge/, #1271) is still the router of its name.
    present = {path.stem for path in (ROOT / SRC).glob("*.py")} | {
        path.parent.name for path in (ROOT / SRC).glob("*/__init__.py")
    }
    assert set(OWNED) <= present, sorted(set(OWNED) - present)


def _served(app: Any) -> set[tuple[str, str, str]]:
    return {
        (method.upper(), path, str(operation.get("operationId", "")))
        for path, item in app.openapi()["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


#: The recorded routes the `software-delivery` plugin serves since round D of ADR 0181 (the release train,
#: the releases, a run's diff, the DORA metrics, the Tekton and Argo CD webhooks): served where its
#: extension is installed, and only there.
SERVED_BY_SOFTWARE_DELIVERY = {
    ("GET", "/api/v1/projects/{id}/metrics/dora", "getProjectDora"),
    ("GET", "/api/v1/projects/{id}/releases", "listReleases"),
    ("GET", "/api/v1/projects/{id}/trains/{env}", "getTrainStatus"),
    ("GET", "/api/v1/releases/{id}", "getRelease"),
    ("GET", "/api/v1/runs/{id}/diff", "getRunDiff"),
    ("POST", "/api/v1/projects/{id}/trains/{env}/depart", "departTrain"),
    ("POST", "/api/v1/projects/{id}/trains/{env}/freeze", "freezeTrain"),
    ("POST", "/api/v1/projects/{id}/trains/{env}/unfreeze", "unfreezeTrain"),
    ("POST", "/api/v1/releases/{id}/abort", "abortRelease"),
    ("POST", "/api/v1/releases/{id}/approve", "approveRelease"),
    ("POST", "/api/v1/webhooks/argocd", "argocdWebhook"),
    ("POST", "/api/v1/webhooks/tekton", "tektonWebhook"),
}


async def test_routes_unchanged(app: Any) -> None:
    """Every (method, path, operationId) served before the translation is still served: by the core, or by
    the `software-delivery` plugin where it is installed (the core alone serves none of the plugin's)."""
    recorded = {(m, p, o) for m, p, o in json.loads(RECORDED.read_text())}
    assert len(recorded) > 300, "the recorded set is the whole API, not a sample"
    assert recorded >= SERVED_BY_SOFTWARE_DELIVERY
    served = _served(app)
    if importlib.util.find_spec("tranibor_software_delivery") is None:
        assert sorted(SERVED_BY_SOFTWARE_DELIVERY & served) == [], "the plugin's routes without the plugin"
        recorded -= SERVED_BY_SOFTWARE_DELIVERY
    missing = recorded - served
    assert sorted(missing) == []
