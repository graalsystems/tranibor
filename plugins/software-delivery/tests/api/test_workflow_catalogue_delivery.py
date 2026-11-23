# SPDX-License-Identifier: Apache-2.0
"""The workflow catalogue's software delivery entries (S49-05, S59-02, ADR 0111), moved from
`apps/api/tests/test_workflow_catalogue.py` (ADR 0181, round D): the `github-software-delivery` template's
workflows and the core's development and audit workflows name the plugin's gates, so they are offered
where its extension is installed. The catalogue's own rules are the core suite's."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

BASE = "/api/v1/orgs/northwind/workflow-catalogue"
ALL = f"{BASE}?limit=200"


def _entry(entries: list[dict[str, Any]], source: str, name: str) -> dict[str, Any]:
    return next(e for e in entries if e["source"] == source and e["name"] == name)


async def test_the_catalogue_lists_the_delivery_template_and_default_simple_with_what_they_need(
    client: AsyncClient, admin: str
) -> None:
    entries = (await client.get(ALL)).json()["items"]
    delivery = _entry(entries, "template:github-software-delivery", "dev-simple")
    assert ("developer", "catalogue") in {(a["slug"], a["resolution"]) for a in delivery["agents"]}, (
        "not installed yet: the platform's catalogue (its offline plugins) provides it"
    )
    assert _entry(entries, "core", "default-simple")["needs_repository"] is True
    assert not any(e["source"].startswith("template:") and e["name"] == "default-simple" for e in entries), (
        "a template's reference to a core workflow is the core entry, not a second one"
    )


async def test_adopting_the_delivery_template_s_workflow_installs_its_agents(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    delivery = await client.post(
        f"/api/v1/projects/{project['id']}/workflow-adoptions",
        json={"source": "template:github-software-delivery", "name": "dev-simple"},
    )
    assert delivery.status_code == 201, delivery.text
    adopted_entry = _entry(
        (await client.get(ALL)).json()["items"], "template:github-software-delivery", "dev-simple"
    )
    assert {a["resolution"] for a in adopted_entry["agents"]} == {"plugin"}, (
        "installed by the adoption, from their plugin"
    )


async def test_the_core_catalogue_offers_development_and_audit_workflows_with_their_agents(
    client: AsyncClient, project: dict[str, Any], admin: str
) -> None:
    """S59-02: the core ships a catalogue (development, audit, decision, documentation, operations), each
    entry presented and its agents resolved; adopting an audit installs the agents it names."""
    entries = (await client.get(ALL)).json()["items"]
    core = {e["name"]: e for e in entries if e["source"] == "core"}
    assert {"dev-feature", "dev-fix", "hotfix", "code-audit", "security-audit", "incident-postmortem"} <= set(
        core
    )
    audit = core["security-audit"]
    assert audit["title"] == "Security audit" and "audit" in audit["tags"]
    assert {a["slug"] for a in audit["agents"]} == {"secops", "security-reviewer", "compliance"}
    assert "unresolved" not in {a["resolution"] for a in audit["agents"]}, "every agent ships with the core"

    adopted = await client.post(
        f"/api/v1/projects/{project['id']}/workflow-adoptions",
        json={"source": "core", "name": "security-audit"},
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["origin"] == "core/security-audit@1"
    agents = (await client.get("/api/v1/orgs/northwind/agents")).json()["items"]
    listed = {a["slug"] for a in (agents["items"] if isinstance(agents, dict) else agents)}
    assert {"secops", "security-reviewer", "compliance"} <= listed, "the adoption installed its agents"
