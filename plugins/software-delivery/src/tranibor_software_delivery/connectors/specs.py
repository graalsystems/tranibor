# SPDX-License-Identifier: Apache-2.0
"""What the `scm`, `ci` and `cd` connector types declare (ADR 0034): their display names, the schemas of their
configuration and their secret fields. They were the core's (`tranibor_adapters.specs`) until round D of
ADR 0181; the strings did not change, so a stored connector reads the same."""

from __future__ import annotations

from typing import Any

from tranibor_adapters.azure_devops import azure_egress, check_azure_pipelines, check_azure_repos
from tranibor_adapters.registry import ConnectorTypeSpec
from tranibor_adapters.specs import _AZURE_CONNECTION, _DEMO_CONFIGURATION


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


_TEXT: dict[str, Any] = {"type": "string"}
_INTEGER: dict[str, Any] = {"type": "integer"}

SPECS: dict[tuple[str, str], ConnectorTypeSpec] = {
    ("scm", "github"): ConnectorTypeSpec(
        "GitHub (App choregos-bot)",
        ("scm",),
        _object_schema(
            {
                "repo": {
                    "type": "string",
                    "format": "github-repo",
                    "description": "owner/repo, picked among the repositories the App can see",
                },
                "merge_queue": {"type": "boolean", "default": True},
                "merge_method": {
                    "type": "string",
                    "enum": ["MERGE", "SQUASH", "REBASE"],
                    "default": "SQUASH",
                },
            },
            ("repo",),
        ),
        github_repository="repo",
    ),
    ("scm", "gitlab"): ConnectorTypeSpec(
        "GitLab",
        ("scm",),
        _object_schema(
            {
                "base_url": {
                    "type": "string",
                    "default": "https://gitlab.com",
                    "description": "self-managed: its URL, with the path prefix if any "
                    "(https://tools.example.org/gitlab); GitLab 15.0 or later",
                },
                "ca_bundle": {
                    "type": "string",
                    "description": "self-managed only: the PEM file of the internal CA that signed the "
                    "instance's certificate (verification is never switched off)",
                },
                "project": _TEXT,
                "default_branch": {"type": "string", "default": "main"},
            },
            ("project",),
        ),
        ("token",),
    ),
    ("scm", "azure-repos"): ConnectorTypeSpec(
        "Azure DevOps Repos",
        ("scm",),
        _object_schema(
            {
                **_AZURE_CONNECTION,
                "repository": {
                    "type": "string",
                    "description": "the Azure Repos Git repository (name or id)",
                },
                "default_branch": {"type": "string", "default": "main"},
                "merge_strategy": {
                    "type": "string",
                    "enum": ["squash", "noFastForward", "rebase", "rebaseMerge"],
                    "default": "squash",
                    "description": "how a pull request is completed; auto-complete is never set and no "
                    "branch policy is bypassed",
                },
                "delete_source_branch": {"type": "boolean", "default": False},
            },
            ("base_url", "project", "repository"),
        ),
        ("pat", "client_secret", "run_token"),
        egress=azure_egress,
        check_config=check_azure_repos,
    ),
    ("ci", "azure-pipelines"): ConnectorTypeSpec(
        "Azure Pipelines",
        ("ci",),
        _object_schema(
            {
                **_AZURE_CONNECTION,
                "repository": {
                    "type": "string",
                    "description": "optional: only the builds of this Azure Repos repository (name or id)",
                },
                "pipeline": {
                    "type": "string",
                    "description": "optional: the pipeline (name or id) whose builds are read and which is "
                    "queued when none is named",
                },
            },
            ("base_url", "project"),
        ),
        ("pat", "client_secret"),
        egress=azure_egress,
        check_config=check_azure_pipelines,
    ),
    ("ci", "tekton"): ConnectorTypeSpec(
        "Tekton Pipelines",
        ("ci",),
        _object_schema({"namespace": _TEXT, "pipeline": {"type": "string", "default": "choregos-ci"}}),
    ),
    ("ci", "gitlab"): ConnectorTypeSpec(
        "GitLab CI",
        ("ci",),
        _object_schema(
            {
                "base_url": {
                    "type": "string",
                    "default": "https://gitlab.com",
                    "description": "self-managed: its URL, with the path prefix if any "
                    "(https://tools.example.org/gitlab); GitLab 15.0 or later",
                },
                "ca_bundle": {
                    "type": "string",
                    "description": "self-managed only: the PEM file of the internal CA that signed the "
                    "instance's certificate (verification is never switched off)",
                },
                "project": _TEXT,
            },
            ("project",),
        ),
        ("token",),
    ),
    ("ci", "jenkins"): ConnectorTypeSpec(
        "Jenkins",
        ("ci",),
        _object_schema(
            {
                "base_url": {"type": "string", "description": "the controller, https://jenkins.example.com"},
                "job": {
                    "type": "string",
                    "description": "the job's path, folder/app; the multibranch folder when `multibranch`",
                },
                "user": {"type": "string", "description": "the user the API token belongs to"},
                "multibranch": {"type": "boolean", "default": False},
                "ref_parameter": {
                    "type": "string",
                    "default": "BRANCH",
                    "description": "the build parameter that receives the ref (unused by a multibranch job)",
                },
                "parameters": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "build parameters sent with every trigger",
                },
                "crumb": {"type": "boolean", "default": True},
            },
            ("base_url", "job", "user"),
        ),
        ("token",),
    ),
    ("ci", "github-actions"): ConnectorTypeSpec(
        "GitHub Actions",
        ("ci",),
        _object_schema(
            {
                "repo": {
                    "type": "string",
                    "format": "github-repo",
                    "description": "owner/repo, picked among the repositories the App can see",
                },
                "workflow": {
                    "type": "string",
                    "description": "the workflow file (ci.yml) to dispatch and to read; empty reads them all",
                },
                "inputs": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "workflow_dispatch inputs sent with every trigger",
                },
                "api_url": {"type": "string", "default": "https://api.github.com"},
            },
            ("repo",),
        ),
        github_repository="repo",
    ),
    ("cd", "argocd"): ConnectorTypeSpec(
        "Argo CD + Rollouts",
        ("cd",),
        _object_schema(
            {
                "gitops_repo": _TEXT,
                "base_url": _TEXT,
                "app_pattern": {"type": "string", "default": "{app}-{env}"},
                "path_prefix": {"type": "string", "default": "apps"},
            },
            ("gitops_repo",),
        ),
        ("token",),
        # Argo CD writes its promotions to the GitOps repository through the App (`cd/argocd.py`).
        github_repository="gitops_repo",
    ),
    # A demonstration tenant's train (S21-24): an in-memory environment, or served by the fakes'
    # process. No operation exposed: it is the train that calls it, never an agent.
    ("cd", "demo"): ConnectorTypeSpec(
        "demo deployment (in memory: demonstrations only, refused in staging and prod)",
        ("cd",),
        _DEMO_CONFIGURATION,
        secret_fields=("api_key",),
        dev_only=True,
    ),
}
