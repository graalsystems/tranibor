# SPDX-License-Identifier: Apache-2.0
"""The publishing identity: who publishes Tranibor and where (ADR 0156).

Where the images and charts are pushed, where the source and its releases live, where the user
documentation is served, where a vulnerability is reported, which address the agent commits as,
and where the platform's own plugins are published. One file holds it, `publishing.json` next to this
module; every runtime use reads it from here, never a literal of its own. The files that cannot read
it (charts, policies, workflows, Dockerfiles, documentation) are listed, checked and rewritten by
`tools/publishing_identity.py`.

The cosign identities are not stored: GitHub's keyless signing names the workflow that signed,
`<repository>/.github/workflows/<file>@<ref>`, so they follow from the repositories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from importlib import resources
from typing import Any

#: The OIDC issuer of GitHub Actions, which signs the images, the binaries and the plugins.
SIGNING_ISSUER = "https://token.actions.githubusercontent.com"
#: The workflow of the source repository that signs a release, and the one that publishes plugins.
RELEASE_WORKFLOW = "release.yml"
PLUGIN_WORKFLOW = "publish.yml"
PLUGIN_BRANCH = "refs/heads/main"
#: The images a release builds, signs and pushes, each as `<registry>/<prefix>-<component>`
#: (the `images` matrix of the release workflow).
IMAGE_COMPONENTS = ("api", "orchestrator", "tools", "web", "runner", "laya", "outpost")
#: The charts a release pushes to `oci://<registry>/charts`, by the suffix of their name: the
#: platform's, named by the prefix alone, and the outpost's, `<prefix>-outpost` (ADR 0147), each
#: packaged from its directory.
CHARTS = {"": "charts/tranibor", "-outpost": "charts/tranibor-outpost"}
#: The product's former name (ADR 0226). The native CLI's files are released under it too for the
#: window of R5 (through 0.31.x): images and charts are no longer published under it (ADR 0176 R3 (e)),
#: but an installed `choregos` binary and its `self update` still look for its own name.
FORMER_CLI_PREFIX = "choregos"
#: The native CLI's platforms (ADR 0151), `(os, arch)` in the manifest's words, and the end of each file's
#: name: the file is `<prefix>-<end>`, `tranibor-linux-x86_64` (ADR 0226 R5), and for the deprecation
#: window also under the former prefix, `choregos-linux-x86_64` (the same bytes).
CLI_PLATFORMS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "arm64"): "linux-arm64",
    ("macos", "arm64"): "macos-arm64",
    ("macos", "x86_64"): "macos-x86_64",
    ("windows", "x86_64"): "windows-x86_64.exe",
}


@dataclass(frozen=True)
class Publishing:
    #: The GitHub organisation that publishes the source, the images and the plugins.
    owner: str
    #: The image and chart namespace: `<registry>/tranibor-api`, `oci://<registry>/charts/tranibor`.
    registry: str
    #: The name every image and chart starts with: `<registry>/<image_prefix>-api`, the charts
    #: `<image_prefix>` and `<image_prefix>-outpost` (ADR 0176 R3). The former prefix, `choregos`, was
    #: published beside it until 0.29.4 and is no longer (step (e)).
    image_prefix: str
    #: The core's repository: its releases, its issues, its files.
    source_repository: str
    #: The public user documentation, without a trailing slash (`TRANIBOR_DOCS_URL` by default).
    documentation: str
    #: Where a vulnerability is reported (`SECURITY.md`).
    security_contact: str
    #: The address the agent's commits are authored with, unless the deployment sets one.
    commit_email: str
    #: The repository the platform's own plugins are built and signed in.
    plugin_repository: str
    #: The signed index of those plugins: the chart's default source.
    plugin_index: str

    @property
    def releases(self) -> str:
        """The release pages, where the CLI binaries are attached."""
        return f"{self.source_repository}/releases"

    def release(self, version: str) -> str:
        """The page of one release: `<releases>/tag/v<version>`."""
        return f"{self.releases}/tag/v{version.removeprefix('v')}"

    def source_file(self, path: str) -> str:
        """A file of the source repository's main branch, as GitHub shows it."""
        return f"{self.source_repository}/blob/main/{path.lstrip('/')}"

    def image(self, name: str, tag: str | None = None) -> str:
        """An image of the registry: `<registry>/<name>[:<tag>]`."""
        return f"{self.registry}/{name}" + (f":{tag}" if tag else "")

    def image_name(self, component: str) -> str:
        """The one name a release pushes an image under: `<registry>/<image_prefix>-<component>`."""
        if component not in IMAGE_COMPONENTS:
            raise ValueError(
                f"{component!r} is not an image the release builds: {', '.join(IMAGE_COMPONENTS)}"
            )
        return self.image(f"{self.image_prefix}-{component}")

    def chart_name(self, suffix: str = "") -> str:
        """The one name a release pushes a chart under (`""` the platform's, `"-outpost"`)."""
        if suffix not in CHARTS:
            raise ValueError(f"{suffix!r} names no chart of the release: {sorted(CHARTS)}")
        return f"{self.image_prefix}{suffix}"

    def cli_file(self, os_name: str, arch: str, *, former: bool = False) -> str:
        """The native CLI's file for a platform: `<image_prefix>-<platform>`, or under the former prefix."""
        try:
            end = CLI_PLATFORMS[(os_name, arch)]
        except KeyError:
            raise ValueError(f"no native CLI is built for {os_name}/{arch}") from None
        return f"{FORMER_CLI_PREFIX if former else self.image_prefix}-{end}"

    def cli_file_names(self, os_name: str, arch: str) -> tuple[str, str]:
        """Both names of a platform's file for the window of ADR 0226 R5: the current one, then the former."""
        return self.cli_file(os_name, arch), self.cli_file(os_name, arch, former=True)

    def cli_twin(self, name: str) -> str | None:
        """The other name of a CLI file (`tranibor-linux-x86_64` <-> `choregos-linux-x86_64`), or None."""
        for current, former in (self.cli_file_names(*platform) for platform in CLI_PLATFORMS):
            if name == current:
                return former
            if name == former:
                return current
        return None

    def signing_identity(self, ref: str, workflow: str = RELEASE_WORKFLOW) -> str:
        """The keyless identity of what a workflow of the source repository signed at `ref`."""
        return f"{self.source_repository}/.github/workflows/{workflow}@{ref}"

    @property
    def plugin_signing_identity(self) -> str:
        """The keyless identity of the platform's plugins: the publish workflow on main."""
        return f"{self.plugin_repository}/.github/workflows/{PLUGIN_WORKFLOW}@{PLUGIN_BRANCH}"


def parse(text: str) -> Publishing:
    """A publishing identity from the text of a `publishing.json`; refuses a missing or empty value."""
    data: dict[str, Any] = json.loads(text)
    names = [field.name for field in fields(Publishing)]
    if "image_alias_prefix" in data:
        raise ValueError(
            "publishing.json: `image_alias_prefix` is gone: the former image and chart names are no "
            "longer published (ADR 0176 R3, step (e)); remove it"
        )
    unknown = sorted(set(data) - set(names) - {"$comment"})
    if unknown:
        raise ValueError(f"publishing.json: unknown key(s) {unknown}")
    values: dict[str, str] = {}
    for name in names:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or value != value.strip() or value.endswith("/"):
            raise ValueError(
                f"publishing.json: `{name}` must be a non-empty string, without surrounding spaces "
                "or a trailing slash"
            )
        values[name] = value
    return Publishing(**values)


def load() -> Publishing:
    """The identity this distribution ships with."""
    return parse(resources.files("tranibor_core").joinpath("publishing.json").read_text(encoding="utf-8"))


PUBLISHING = load()
