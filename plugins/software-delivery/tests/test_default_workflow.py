"""A project without a template gets `default-simple` only where the plugin's extension is installed
(ADR 0181, round D).

`default-simple` delivers code: its gates and its `open_pr`/`merge_pr` are the `software-delivery`
plugin's. Without the extension it cannot be published, so a project that names a repository and no
template gets `default-request`, which the core runs by itself; with the extension nothing changes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tranibor_core import gates, step_effects
from tranibor_software_delivery import NAME
from tranibor_software_delivery.extension import register


@pytest.fixture
def without_the_extension() -> Iterator[None]:
    gates.forget(NAME)
    step_effects.forget(NAME)
    try:
        yield
    finally:
        register()


def _names(has_repository: bool) -> list[str]:
    from tranibor_api.services.templates import delivery
    from tranibor_core import parse_workflow

    return [
        parse_workflow(source, strict=False)[0].metadata.name
        for source in delivery({}, None, has_repository=has_repository).workflows
    ]


def test_with_the_extension_a_repository_gets_default_simple() -> None:
    assert _names(True) == ["default-simple"]
    assert _names(False) == ["default-request"]


def test_without_the_extension_a_repository_gets_default_request(without_the_extension: None) -> None:
    assert _names(True) == ["default-request"]
    assert _names(False) == ["default-request"]


def test_a_template_s_own_workflows_still_win(without_the_extension: None) -> None:
    from tranibor_api.services.templates import delivery
    from tranibor_core import template_yaml

    shipped = delivery({"defaults": {"workflow": "template:default-simple@1"}}, None).workflows
    assert shipped == [template_yaml("default-simple")]
