# SPDX-License-Identifier: Apache-2.0
"""The provisioning steps of software delivery (ADR 0181, round D): those that write to and read from a
project's `cd` connector, run by the core's `run_provision_step` through the worker seam
(`WorkerParts.provisioning_steps`). The names are the ones the templates' manifests carry.

- `gitops.write_project_manifests`: the project's manifests written into the GitOps repository, which
  Argo CD applies (the API never applies them);
- `argocd.wait_synced`: the project's Argo CD applications are synced.

Each is replayed when a provisioning resumes, and creates nothing twice (ADR 0008): the writer overwrites
the same files, the check reads.
"""

from __future__ import annotations

from typing import Any

from tranibor_core.worker_parts import ProvisioningStep

from .connectors import cd_of


async def write_project_manifests(params: dict[str, Any], bundle: Any, settings: Any) -> str:
    """Write the project manifests into the GitOps repository: Argo CD applies, not the API."""
    from tranibor_core.dns_policy import UPSTREAM
    from tranibor_core.quotas import quota_for
    from tranibor_orchestrator.activities.base import db
    from tranibor_orchestrator.gitops import EGRESS_IMAGE, PLATFORM_NAMESPACE, render_project_manifests
    from tranibor_orchestrator.org_egress import org_runner_hosts

    path = str(params.get("path", "projects/{{slug}}/")).replace("{{slug}}", bundle.slug)
    org_id = getattr(getattr(bundle, "project", None), "org_id", None)
    org_hosts: list[str] = []
    if org_id:
        async with db() as session:
            org_hosts = await org_runner_hosts(session, org_id)
    manifests = render_project_manifests(
        bundle.slug,
        bundle.config,
        bundle.policy,
        egress_image=str(getattr(settings, "egress_image", "") or EGRESS_IMAGE),
        quota=await quota_for(bundle.org_slug, bundle.slug),
        org_allow_domains=org_hosts,
        platform_namespace=str(getattr(settings, "platform_namespace", "") or PLATFORM_NAMESPACE),
        dns=getattr(settings, "dns_policy", None) or UPSTREAM,
    )
    writer = getattr(cd_of(bundle.adapters), "write_files", None)
    if writer is not None:
        await writer(path, manifests)
    return f"{len(manifests)} manifest(s) written to {path}"


async def wait_synced(params: dict[str, Any], bundle: Any, settings: Any) -> str:
    apps = params.get("apps", [f"choregos-project-{bundle.slug}"])
    for app in apps:
        health = await cd_of(bundle.adapters).health(str(app).replace("{{slug}}", bundle.slug))
        if health.status in {"Degraded", "Missing"}:
            raise RuntimeError(f"application {app} is not synced: {health.status}")
    return f"{len(apps)} application(s) synced"


STEPS: dict[str, ProvisioningStep] = {
    "gitops.write_project_manifests": ProvisioningStep(
        write_project_manifests, "Check the write rights on `choregos-infra`."
    ),
    "argocd.wait_synced": ProvisioningStep(
        wait_synced, "Look at the application in Argo CD: a manual sync is possible."
    ),
}
