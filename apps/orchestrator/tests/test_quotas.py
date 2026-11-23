# SPDX-License-Identifier: Apache-2.0
"""Does the quota of a project's namespaces come from the extension (per organisation), else the default?

The extension is really installed (`.dist-info`); `load_extensions()` finds it, and the quota is read
by `quota_for` and rendered into the project's manifests (`gitops.render_project_manifests`), what the
`software-delivery` plugin's provisioning step writes into the GitOps repository
(`plugins/software-delivery/tests/test_provisioning_steps.py`).
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from tranibor_contracts import ProjectConfig
from tranibor_core import load_preset

EXTENSION = """
from tranibor_core.quotas import Quota, declare_quota

def quota(org, project):
    return Quota(cpu="4", memory="8Gi", pods=6) if org == "small" else None

def register():
    declare_quota(quota)
"""


@pytest.fixture
def extension(tmp_path: pathlib.Path) -> Iterator[None]:
    from tranibor_adapters import load_extensions
    from tranibor_core.quotas import reset

    root = tmp_path / "site"
    root.mkdir()
    (root / "extension_quota.py").write_text(EXTENSION, encoding="utf-8")
    info = root / "extension_quota-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: extension_quota\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        "[choregos.plugins]\nextension_quota = extension_quota:register\n", encoding="utf-8"
    )
    sys.path.insert(0, str(root))
    try:
        assert "extension_quota" in load_extensions()
        yield
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("extension_quota", None)
        reset()


async def _written_quotas(org: str) -> list[dict[str, Any]]:
    """Renders the project's manifests with its quota, and returns the `ResourceQuota` rendered."""
    from tranibor_core.quotas import quota_for
    from tranibor_orchestrator.gitops import render_project_manifests

    config = ProjectConfig.model_validate(
        {
            "slug": "billing",
            "org": org,
            "delivery": {"repo": {"url": "https://github.com/x/billing.git", "default_branch": "main"}},
        }
    )
    written = render_project_manifests(
        "billing", config, load_preset("team"), quota=await quota_for(org, "billing")
    )
    documents = [d for text in written.values() for d in yaml.safe_load_all(text) if d]
    return [d for d in documents if d["kind"] == "ResourceQuota"]


async def test_the_extension_sets_the_quota_of_its_organisation(extension: None) -> None:
    quotas = await _written_quotas("small")
    assert len(quotas) == 2, "one quota per namespace (runners, ci)"
    for quota in quotas:
        hard = quota["spec"]["hard"]
        assert (hard["requests.cpu"], hard["limits.memory"], hard["count/pods"]) == ("4", "8Gi", "6")


async def test_another_organisation_keeps_the_default(extension: None) -> None:
    for quota in await _written_quotas("large"):
        hard = quota["spec"]["hard"]
        assert (hard["requests.cpu"], hard["limits.memory"], hard["count/pods"]) == ("32", "96Gi", "40")


async def test_without_an_extension_the_historical_default() -> None:
    for quota in await _written_quotas("small"):
        assert quota["spec"]["hard"]["requests.cpu"] == "32"


@pytest.mark.parametrize(
    ("cpu", "memory", "pods"), [("plenty", "8Gi", 6), ("4", "8 gigas", 6), ("4", "8Gi", 0)]
)
def test_a_malformed_quota_is_refused_before_argo(cpu: str, memory: str, pods: int) -> None:
    from tranibor_core.quotas import Quota

    with pytest.raises(ValueError, match="quota"):
        Quota(cpu=cpu, memory=memory, pods=pods)
