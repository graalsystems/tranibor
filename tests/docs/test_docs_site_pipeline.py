"""The documentation site is built and published from this repository (ADR 0185).

The publishing identity says where the documentation lives; the Docusaurus project and the workflow must
agree with it, or the console's Help link points at a site that nothing publishes.
"""

from __future__ import annotations

import json
import pathlib
import re
from urllib.parse import urlparse

from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEBSITE = ROOT / "website"
WORKFLOW = ROOT / ".github" / "workflows" / "docs-site.yml"
IDENTITY = json.loads((ROOT / "packages/core/src/tranibor_core/publishing.json").read_text("utf-8"))


def test_the_site_is_served_where_the_publishing_identity_says() -> None:
    """The Docusaurus config reads the identity: it states no address of its own."""
    config = (WEBSITE / "docusaurus.config.ts").read_text("utf-8")
    assert "publishing.json" in config and "identity.documentation" in config
    for value in IDENTITY.values():
        if value.startswith("https://") and value != IDENTITY["documentation"]:
            assert value not in config, f"the config spells {value}: it reads the identity"
    assert "graalsystems" not in config and "northwind" not in config.lower()


def test_the_documentation_is_not_hosted_by_a_third_party() -> None:
    host = urlparse(IDENTITY["documentation"]).hostname or ""
    owner = IDENTITY["owner"]
    assert host == f"{owner}.github.io", f"{host} is not the GitHub Pages of {owner}"
    assert urlparse(IDENTITY["documentation"]).path == "/" + IDENTITY["source_repository"].rsplit("/", 1)[1]


def test_the_site_reads_the_pages_of_this_repository() -> None:
    assert (ROOT / "docs" / "site" / "index.md").is_file()
    assert (WEBSITE / "package-lock.json").is_file(), "the workflow installs with `npm ci`"
    script = (WEBSITE / "scripts" / "import-docs.mjs").read_text("utf-8")
    assert "publishing.json" in script and "source_repository" in script
    assert "graalsystems" not in script


def test_the_workflow_builds_on_pull_requests_and_publishes_from_main_only() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert "pull_request:" in text and "branches: [main]" in text
    # Not on a release: the `github-pages` environment deploys from main only, so a tag's deploy fails
    # (v0.29.4), and a release the release workflow publishes starts no workflow (ADR 0185, amended).
    triggers = text.split("\npermissions:", 1)[0]
    assert "\n  release:" not in triggers and "github.event.release" not in text
    assert "actions/deploy-pages@" in text and "id-token: write" in text
    # The publishing job never runs for a pull request.
    assert re.search(r"deploy:\n\s+if: github\.event_name != 'pull_request'", text)


def _steps() -> list[str]:
    """The build job's steps, each as its text, in order."""
    text = WORKFLOW.read_text("utf-8")
    build = text.split("\n  build:\n", 1)[1].split("\n  deploy:\n", 1)[0]
    return [chunk for chunk in re.split(r"\n(?=      - )", build) if chunk.lstrip().startswith("- ")]


def test_the_connector_pages_are_written_after_the_import_and_before_the_build() -> None:
    """S84-06 (ADR 0215): the import empties website/docs/ first, so the pages are written after it, and
    the build publishes them; each assertion fails if its step is removed or moved."""
    steps = _steps()
    at = {
        name: next(i for i, step in enumerate(steps) if marker in step)
        for name, marker in (
            ("import", "scripts/import-docs.mjs"),
            ("export", "tools/procedures/export_docs.py --site docs/connectors"),
            ("build", "npm run build"),
        )
    }
    assert at["import"] < at["export"] < at["build"]
    export = steps[at["export"]]
    assert "--plugins ../.plugins-checkout/plugins" in export and "--plugins-ref" in export
    assert "--plugins-lenient" in export and "github.event_name != 'pull_request'" in export


def test_the_plugins_are_read_from_their_public_repository_without_credentials() -> None:
    checkout = next(step for step in _steps() if "path: .plugins-checkout" in step)
    plugins = urlparse(PUBLISHING.plugin_repository).path.strip("/")
    assert f"repository: {plugins}" in checkout and "ref: main" in checkout
    assert "persist-credentials: false" in checkout


def test_a_published_plugin_procedure_reaches_the_site_without_a_commit_here() -> None:
    text = WORKFLOW.read_text("utf-8")
    assert re.search(r"\n  schedule:\n    - cron: ", text), "a daily build picks up the plugins' procedures"
