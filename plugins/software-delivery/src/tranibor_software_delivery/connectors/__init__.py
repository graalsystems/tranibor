# SPDX-License-Identifier: Apache-2.0
"""The connector types of software delivery (ADR 0181, round D): source control (`scm`: GitHub, GitLab,
Azure Repos), continuous integration (`ci`: Tekton, GitLab CI, GitHub Actions, Jenkins, Azure Pipelines)
and continuous deployment (`cd`: Argo CD, and the demonstration environment), with their fakes.

They were the core's (`tranibor_adapters.scm`, `.ci`, `.cd`, `.fakes`) until round D. The extension registers
them through the core's seams when it loads, under the same kinds and type names, so a stored connector
builds the same adapter:

- `tranibor_adapters.register(kind, type, spec, plugin=)` for each type and its fake;
- `tranibor_adapters.declare_adapter_slot(kind, ...)`: every project's `AdapterSet` gets an `scm`, a `ci`
  and a `cd` adapter, built from the project's connector of that kind (`github`, `tekton` and `argocd`
  when it has none), read with `scm_of`, `ci_of` and `cd_of`;
- `tranibor_adapters.fakes.hr.declare_demo_desk`: the fakes' process serves the demonstration `cd`.
"""

from __future__ import annotations

from typing import Any, cast

from tranibor_adapters import AdapterSet, declare_adapter_slot, register
from tranibor_adapters.fakes.hr import declare_demo_desk

from ..names import NAME
from .base import CdAdapter, CiAdapter, ScmAdapter
from .specs import SPECS

#: The type a project that declares no connector of the kind gets.
DEFAULT_SCM, DEFAULT_CI, DEFAULT_CD = "github", "tekton", "argocd"


def scm_of(adapters: AdapterSet) -> ScmAdapter:
    """The project's source control (the work item's target may need another: `scm.scm_for`)."""
    return cast(ScmAdapter, adapters.slot("scm"))


def ci_of(adapters: AdapterSet) -> CiAdapter:
    """The project's continuous integration."""
    return cast(CiAdapter, adapters.slot("ci"))


def cd_of(adapters: AdapterSet) -> CdAdapter:
    """The project's continuous deployment."""
    return cast(CdAdapter, adapters.slot("cd"))


def _env(name: str, default: str = "") -> str:
    import os

    return os.environ.get(name, default)


def _github_scm(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.builtins import _github_client

    from .scm.github import GitHubScm

    return GitHubScm(
        _github_client(cfg),
        default_repo=cfg.get("repo", ""),
        merge_method=cfg.get("merge_method", "SQUASH"),
        use_merge_queue=cfg.get("merge_queue", True),
    )


def _gitlab_scm(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.tracker.gitlab_instance import build_client

    from .scm.gitlab import GitLabScm

    return GitLabScm(build_client(cfg), cfg["project"], default_branch=cfg.get("default_branch", "main"))


def _azure_repos_scm(cfg: dict[str, Any]) -> Any:
    """Azure DevOps Repos: a PAT or an Entra service principal, secrets from the vault. The run token is a
    separate, narrower PAT: a run never receives the connector's own credential."""
    from tranibor_adapters.azure_devops import azure_client, check_azure_repos

    from .scm.azure_repos import AzureReposScm

    check_azure_repos(cfg)
    return AzureReposScm(
        azure_client(cfg, service="azure-repos"),
        cfg["project"],
        cfg["repository"],
        default_branch=cfg.get("default_branch", "main"),
        merge_strategy=cfg.get("merge_strategy", "squash"),
        delete_source_branch=bool(cfg.get("delete_source_branch", False)),
        run_token=str(cfg.get("run_token") or ""),
        partially_succeeded=cfg.get("partially_succeeded", "failure"),
    )


def _azure_pipelines_ci(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.azure_devops import azure_client, check_azure_pipelines

    from .ci.azure_pipelines import AzurePipelinesCi

    check_azure_pipelines(cfg)
    return AzurePipelinesCi(
        azure_client(cfg, service="azure-pipelines"),
        cfg["project"],
        repository=str(cfg.get("repository") or ""),
        pipeline=str(cfg.get("pipeline") or ""),
        partially_succeeded=cfg.get("partially_succeeded", "failure"),
    )


def _tekton_ci(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.executor.tekton import KubernetesClient

    from .ci.tekton import TektonCi

    return TektonCi(
        KubernetesClient(**cfg.get("kubernetes", {})),
        namespace=cfg.get("namespace", "default"),
        pipeline=cfg.get("pipeline", "choregos-ci"),
    )


def _gitlab_ci(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.tracker.gitlab_instance import build_client

    from .ci.gitlab import GitLabCi

    return GitLabCi(build_client(cfg), cfg["project"])


def _jenkins_ci(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.http import RestClient

    from .ci.jenkins import JenkinsCi

    return JenkinsCi(
        RestClient(cfg["base_url"], auth=(cfg["user"], cfg["token"]), service="jenkins"),
        cfg["job"],
        multibranch=bool(cfg.get("multibranch", False)),
        parameters=cfg.get("parameters"),
        ref_parameter=cfg.get("ref_parameter", "BRANCH"),
        crumb=bool(cfg.get("crumb", True)),
    )


def _github_actions_ci(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.builtins import _github_client

    from .ci.github_actions import GitHubActionsCi

    return GitHubActionsCi(
        _github_client(cfg), cfg["repo"], workflow=cfg.get("workflow", ""), inputs=cfg.get("inputs")
    )


def _argocd_cd(cfg: dict[str, Any]) -> Any:
    from tranibor_adapters.builtins import _github_client

    from .cd.argocd import ArgoCdAdapter

    return ArgoCdAdapter(
        base_url=cfg.get("base_url", "http://argocd-server.argocd"),
        token=cfg.get("token", ""),
        gitops_repo=cfg.get("gitops_repo", ""),
        github=_github_client(cfg),
        app_pattern=cfg.get("app_pattern", "{app}-{env}"),
        path_prefix=cfg.get("path_prefix", "apps"),
    )


def _demo_cd(cfg: dict[str, Any]) -> Any:
    """The factory of `cd: demo`: refused in staging and prod; served when it carries a `url`."""
    import tranibor_adapters as package
    from tranibor_adapters.errors import ConfigurationError
    from tranibor_adapters.fakes.hr import Desk, RemoteDesk

    from .fakes.demo_cd import DemoCd, FakeDemoCd

    environment = _env("TRANIBOR_ENV", "dev")
    if environment in {"staging", "prod"}:
        raise ConfigurationError(
            f"connector `cd: demo` refused in {environment}: a simulated environment, a "
            "demonstration, not a deployment."
        )
    if cfg.get("url"):
        from tranibor_adapters.mcp import McpClient

        return DemoCd(RemoteDesk(McpClient(str(cfg["url"]), token=str(cfg.get("api_key", "")))))
    desks = package.FAUX_METIER  # lang: fr-ok (the business fakes keep their name)
    if "cd" not in desks:
        desks["cd"] = FakeDemoCd()
    return DemoCd(Desk(desks["cd"], str(cfg.get("api_key", ""))))


def _fake_scm(cfg: dict[str, Any]) -> Any:
    from .fakes.scm import FakeScm

    return FakeScm()


def _fake_ci(cfg: dict[str, Any]) -> Any:
    from .fakes.ci import FakeCi

    return FakeCi()


def _fake_cd(cfg: dict[str, Any]) -> Any:
    from .fakes.cd import FakeCd

    return FakeCd()


FACTORIES = {
    ("scm", "github"): _github_scm,
    ("scm", "gitlab"): _gitlab_scm,
    ("scm", "azure-repos"): _azure_repos_scm,
    ("ci", "azure-pipelines"): _azure_pipelines_ci,
    ("ci", "tekton"): _tekton_ci,
    ("ci", "gitlab"): _gitlab_ci,
    ("ci", "jenkins"): _jenkins_ci,
    ("ci", "github-actions"): _github_actions_ci,
    ("cd", "argocd"): _argocd_cd,
    ("cd", "demo"): _demo_cd,
}


def register_connectors() -> None:
    """Register the `scm`, `ci` and `cd` types, their fakes and their slots. Idempotent."""
    from .fakes.cd import FakeCd
    from .fakes.ci import FakeCi
    from .fakes.demo_cd import FakeDemoCd
    from .fakes.scm import FakeScm

    for (kind, type_name), factory in FACTORIES.items():
        register(kind, type_name, SPECS[(kind, type_name)], plugin=NAME)(factory)
    register("scm", "fake", plugin=NAME)(_fake_scm)
    register("ci", "fake", plugin=NAME)(_fake_ci)
    register("cd", "fake", plugin=NAME)(_fake_cd)
    # The source control is the project's workspace: a workflow that needs it needs a repository.
    declare_adapter_slot(
        "scm",
        plugin=NAME,
        default_type=DEFAULT_SCM,
        fake=FakeScm,
        title="Repository (SCM)",
        family="Source code",
        workspace=True,
    )
    # A Tekton pipeline's cluster is the deployment's to name, never a connector's (S65-03).
    declare_adapter_slot(
        "ci",
        plugin=NAME,
        default_type=DEFAULT_CI,
        fake=FakeCi,
        deployment_fields=frozenset({"kubernetes"}),
        title="Continuous integration",
        family="CI",
        needs_workspace=True,
    )
    declare_adapter_slot(
        "cd",
        plugin=NAME,
        default_type=DEFAULT_CD,
        fake=FakeCd,
        title="Continuous delivery",
        family="Deployment",
        needs_workspace=True,
    )
    declare_demo_desk("cd", FakeDemoCd, plugin=NAME)


__all__ = [
    "DEFAULT_CD",
    "DEFAULT_CI",
    "DEFAULT_SCM",
    "CdAdapter",
    "CiAdapter",
    "ScmAdapter",
    "cd_of",
    "ci_of",
    "register_connectors",
    "scm_of",
]
