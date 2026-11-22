# SPDX-License-Identifier: Apache-2.0
"""GitHub Actions and Argo CD are pinned to the installation their organisation claimed, as the core's GitHub
connectors are (ADR 0073). They were `apps/api/tests/test_github_connector_installation.py`'s; the types are
the `software-delivery` plugin's since round D of ADR 0181."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

PUT = "/api/v1/projects/{id}/connectors/{kind}"


async def _put(
    client: AsyncClient, project: dict[str, Any], kind: str, type_: str, config: dict[str, Any]
) -> Any:
    return await client.put(PUT.format(id=project["id"], kind=kind), json={"type": type_, "config": config})


async def test_the_github_actions_ci_is_pinned_like_the_other_github_connectors(
    client: AsyncClient, project: dict[str, Any], github: Any
) -> None:
    """`ci/github-actions` is served by the App: on a multi-organisation instance organisation B cannot
    save it on, nor run it through, the installation organisation A claimed (ADR 0073)."""
    from sqlalchemy import select
    from tranibor_adapters.errors import ConfigurationError
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import github_app
    from tranibor_testkit.github_app import add_organisation, claim_for

    async with session_scope(orgs="*") as session:
        mine = (await session.execute(select(Project.org_id).where(Project.id == project["id"]))).scalar_one()
    globex = await add_organisation("globex")
    github.add_installation(1, "globex-inc")
    await claim_for(1, "globex-inc", globex)
    github.add_installation(2, "northwind")
    await claim_for(2, "northwind", str(mine))

    refused = await _put(client, project, "ci", "github-actions", {"repo": "globex-inc/private-repo"})
    assert refused.status_code == 422, refused.text
    accepted = await _put(client, project, "ci", "github-actions", {"repo": "northwind/billing"})
    assert accepted.status_code == 200, accepted.text
    assert "installation_id" not in accepted.json()["config"]

    async with session_scope(orgs="*") as session:
        pinned = await github_app.pin_installation(
            session, str(mine), "ci", "github-actions", {"repo": "northwind/billing"}
        )
        assert pinned["installation_id"] == 2
        with pytest.raises(ConfigurationError, match="not installed on `globex-inc`"):
            await github_app.pin_for_runtime(
                session, str(mine), "ci", "github-actions", {"repo": "globex-inc/private-repo"}
            )


async def test_the_argocd_gitops_repository_is_pinned_like_the_other_github_connectors(
    client: AsyncClient, project: dict[str, Any], github: Any
) -> None:
    """`cd/argocd` writes its promotions to `gitops_repo` through the App: on a multi-organisation
    instance organisation B cannot save it on, nor promote through, the installation organisation A
    claimed (ADR 0073, finding #2033)."""
    from sqlalchemy import select
    from tranibor_adapters.errors import ConfigurationError
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import github_app
    from tranibor_testkit.github_app import add_organisation, claim_for

    async with session_scope(orgs="*") as session:
        mine = (await session.execute(select(Project.org_id).where(Project.id == project["id"]))).scalar_one()
    globex = await add_organisation("globex")
    github.add_installation(1, "globex-inc")
    await claim_for(1, "globex-inc", globex)
    github.add_installation(2, "northwind")
    await claim_for(2, "northwind", str(mine))

    refused = await _put(client, project, "cd", "argocd", {"gitops_repo": "globex-inc/gitops"})
    assert refused.status_code == 422, refused.text
    assert "not installed on `globex-inc`" in refused.text
    accepted = await _put(client, project, "cd", "argocd", {"gitops_repo": "northwind/gitops"})
    assert accepted.status_code == 200, accepted.text
    assert "installation_id" not in accepted.json()["config"]

    async with session_scope(orgs="*") as session:
        pinned = await github_app.pin_installation(
            session, str(mine), "cd", "argocd", {"gitops_repo": "northwind/gitops"}
        )
        assert pinned["installation_id"] == 2
        with pytest.raises(ConfigurationError, match="not installed on `globex-inc`"):
            await github_app.pin_for_runtime(
                session, str(mine), "cd", "argocd", {"gitops_repo": "globex-inc/gitops"}
            )
