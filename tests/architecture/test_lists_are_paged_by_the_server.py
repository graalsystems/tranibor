# SPDX-License-Identifier: Apache-2.0
"""ADR 0206: a list the API returns is paged by the server, in SQL — two ratchets.

1. Every `GET` of the contract whose `200` is a bare JSON array is named below: either a list bounded by
   construction (`BOUNDED`, a reason each) or a list still to migrate (`NOT_YET_PAGED`, the ceiling).
   A new array route fails here; a migrated one must leave the ceiling in the same PR (ADR 0099).
2. `Page.slice` — read up to a cap, cut in Python — is counted, and the count may only go down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tranibor_contracts as contracts

ROUTERS = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "tranibor_api" / "routers"

#: Lists whose length is fixed by the code or the deployment, not by what users create. Never paged.
BOUNDED: dict[str, str] = {
    "listAdminSections": "the manifest's sections (ADR 0032)",
    "listBackends": "the agent backends the code knows",
    "listConnectorTypes": "the connector types the code registers",
    "listConnectors": (
        "one row per connector kind: `UniqueConstraint(project_id, kind)` "
        "(apps/api/src/tranibor_api/db/models/projects.py:90), and `putConnector` refuses a (kind, type) the "
        "registry does not hold (apps/api/src/tranibor_api/services/connectors.py:66-73)"
    ),
    "listProjectRequirements": (
        "one entry per capability, never per row: `requirements()` groups reasons by capability "
        "(packages/core/src/tranibor_core/dsl/requirements.py:121-133); the keys are the fixed ORDER plus "
        "the connector kinds a workflow's `needs:` names, validated against the registered kinds "
        "(packages/contracts/src/tranibor_contracts/open_lists.py:110, workflow.py:79); derived in Python, "
        "so it has no SQL order to keyset on"
    ),
    "listExecutors": "the five executor kinds",
    "listProcedures": "the shipped procedures",
    "listTemplates": "the shipped project templates",
    "listWorkflowTemplates": "the shipped workflow templates",
}

#: The ceiling: array routes that read without a page. Lowered when one migrates, never raised.
NOT_YET_PAGED: frozenset[str] = frozenset(
    {
        "getRunEvents",
        "getRunInputs",
        "getRunKnowledge",
        "getRunSkills",
        "getWorkItemHandOffs",
        "getWorkItemTimeline",
        "listAgentCatalogue",
        "listAgentCredentials",
        "listAgentEvaluations",
        "listAgentFeedbackCandidates",
        "listAgentImages",
        "listAgentMetrics",
        "listAgentReleases",
        "listAgentRuns",
        "listAgents",
        "listBudgetAlerts",
        "listGlobalPendingMemory",
        "listImplicitAgents",
        "listKnowledgeBases",
        "listMachineAccounts",
        "listMemberLinkedAccounts",
        "listMembers",
        "listMemories",
        "listMyTokens",
        "listMySessions",
        "listOrgAgentCredentials",
        "listOrgAgentReleases",
        "listOrgs",
        "listPendingMemory",
        "listPlatformModels",
        "listProjectAgents",
        "listProjectMemories",
        "listRemoteCredentials",
        "listRuns",
        "listSkills",
        "listTrackerIdentities",
        "listTriggers",
        "listUsableModels",
        "listWorkItemArtifacts",
        "listWorkflowAgents",
        "listWorkflowCatalogue",
        "listWorkflowVersions",
        "listWorkflows",
        "resolveProjectAgents",
        "searchGlobalMemory",
        "searchMemory",
    }
)

#: The ceiling of `page.slice(` calls in the routers (in-memory pages). Lowered when one migrates.
SLICE_CEILING = 8


def _array_routes() -> set[str]:
    found = set()
    for operations in contracts.load_openapi()["paths"].values():
        get: dict[str, Any] | None = operations.get("get")
        if get is None:
            continue
        schema = (
            get.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        if schema.get("type") == "array":
            found.add(get["operationId"])
    return found


def test_no_new_list_route_returns_a_bare_array() -> None:
    known = set(BOUNDED) | NOT_YET_PAGED
    new = _array_routes() - known
    assert not new, (
        f"{sorted(new)} return a bare array: return `{{items, meta}}` with `cursor`/`limit` and page in "
        "SQL (`tranibor_api.keyset.keyset_page`, ADR 0206), or, for a list fixed by the code, add it to "
        "BOUNDED with why"
    )


def test_the_ceiling_drops_when_a_route_is_paged() -> None:
    stale = (set(BOUNDED) | NOT_YET_PAGED) - _array_routes()
    assert not stale, f"{sorted(stale)} no longer return an array (or are gone): remove them from this file"
    assert not set(BOUNDED) & NOT_YET_PAGED


def test_in_memory_pages_only_decrease() -> None:
    count = sum(path.read_text().count("page.slice(") for path in ROUTERS.rglob("*.py"))
    assert count <= SLICE_CEILING, (
        f"{count} `page.slice(` calls, ceiling {SLICE_CEILING}: page in SQL (ADR 0206)"
    )
    assert count == SLICE_CEILING, f"{count} `page.slice(` calls: lower SLICE_CEILING to {count}"
