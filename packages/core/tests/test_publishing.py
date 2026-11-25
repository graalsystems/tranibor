# SPDX-License-Identifier: Apache-2.0
"""`tranibor_core.publishing`: the one definition of the publishing identity (ADR 0156)."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from tranibor_core.publishing import PUBLISHING, SIGNING_ISSUER, load, parse

EXAMPLE = {
    "owner": "ExampleOrg",
    "registry": "registry.example.org/exampleorg",
    "image_prefix": "exampleware",
    "source_repository": "https://forge.example.org/exampleorg/choregos",
    "documentation": "https://docs.example.org/choregos",
    "security_contact": "security@example.org",
    "commit_email": "bot@example.org",
    "plugin_repository": "https://forge.example.org/exampleorg/choregos-agents",
    "plugin_index": "oci://registry.example.org/exampleorg/choregos-agents/index:latest",
}


def test_the_shipped_definition_loads_and_is_the_module_s() -> None:
    assert load() == PUBLISHING
    assert all(value for value in asdict(PUBLISHING).values())


def test_what_follows_from_the_repositories() -> None:
    identity = parse(json.dumps({"$comment": "kept", **EXAMPLE}))
    assert identity.releases == "https://forge.example.org/exampleorg/choregos/releases"
    assert identity.release("v1.2.3") == identity.release("1.2.3") == f"{identity.releases}/tag/v1.2.3"
    assert (
        identity.source_file("/docs/x.md")
        == "https://forge.example.org/exampleorg/choregos/blob/main/docs/x.md"
    )
    assert identity.image("exampleware-api") == "registry.example.org/exampleorg/exampleware-api"
    assert (
        identity.image("exampleware-api", "1.0.0") == "registry.example.org/exampleorg/exampleware-api:1.0.0"
    )
    assert identity.signing_identity("refs/tags/v1.0.0") == (
        "https://forge.example.org/exampleorg/choregos/.github/workflows/release.yml@refs/tags/v1.0.0"
    )
    assert identity.plugin_signing_identity == (
        "https://forge.example.org/exampleorg/choregos-agents/.github/workflows/publish.yml@refs/heads/main"
    )
    assert SIGNING_ISSUER.startswith("https://")


def test_one_image_and_one_chart_under_one_name() -> None:
    """ADR 0176 R3 (e): a release publishes each image and each chart under `image_prefix` only."""
    identity = parse(json.dumps(EXAMPLE))
    assert identity.image_name("api") == "registry.example.org/exampleorg/exampleware-api"
    assert identity.chart_name() == "exampleware"
    assert identity.chart_name("-outpost") == "exampleware-outpost"
    # quant is a command of the runner image, not an image (#1847).
    with pytest.raises(ValueError, match="not an image the release builds"):
        identity.image_name("quant")
    with pytest.raises(ValueError, match="names no chart"):
        identity.chart_name("-web")
    assert PUBLISHING.image_name("runner") == PUBLISHING.image(f"{PUBLISHING.image_prefix}-runner")
    assert not hasattr(PUBLISHING, "image_alias_prefix")


def test_the_former_alias_prefix_is_refused_by_name() -> None:
    """A definition still carrying `image_alias_prefix` is told why, not just that the key is unknown."""
    with pytest.raises(ValueError, match="no longer published"):
        parse(json.dumps(EXAMPLE | {"image_alias_prefix": "examplelegacy"}))


@pytest.mark.parametrize(
    "change",
    [
        {"registry": ""},
        {"registry": " registry.example.org"},
        {"documentation": "https://docs.example.org/choregos/"},
        {"owner": 3},
        {"unknown": "x"},
    ],
)
def test_a_value_that_would_not_compose_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"publishing\.json"):
        parse(json.dumps(EXAMPLE | change))


def test_a_missing_value_is_refused() -> None:
    with pytest.raises(ValueError, match="`plugin_index`"):
        parse(json.dumps({key: value for key, value in EXAMPLE.items() if key != "plugin_index"}))
