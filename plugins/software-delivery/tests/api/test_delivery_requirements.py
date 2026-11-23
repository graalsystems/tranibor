# SPDX-License-Identifier: Apache-2.0
"""What a software project's default workflow asks of its connectors (ADR 0034, S19-01, S40-05).

The delivery half of `apps/api/tests/test_connectors_by_capability.py`: `default-simple`, which a project
with a repository gets where the `software-delivery` plugin's extension is installed, needs a repository
(`scm`) and a release train (`cd`), both the plugin's connector kinds since round D of ADR 0181.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient


async def test_the_default_template_delivers_software_and_reads_no_ci(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    pid = project["id"]
    assert project["workflow_name"] == "default-simple"
    software = [r["capability"] for r in (await client.get(f"/api/v1/projects/{pid}/requirements")).json()]
    assert {"scm", "cd"} <= set(software), "the default template delivers software"
    assert "ci" not in software, "the PR checks are read through the repository (S21-23)"


async def test_neither_the_repository_nor_the_train_is_a_platform_default(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S40-05: `cd` said "Platform default: argocd" and `ci` "tekton" with nothing behind them."""
    monkeypatch.setenv("TRANIBOR_EXECUTOR_KIND", "k8s_job")
    pid = project["id"]
    items = {r["capability"]: r for r in (await client.get(f"/api/v1/projects/{pid}/requirements")).json()}
    assert {"scm", "cd", "runtime"} <= set(items)
    assert items["cd"]["default_type"] is None and items["scm"]["default_type"] is None
    assert items["runtime"]["default_type"] == "k8s_job"
