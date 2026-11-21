"""The carrier seam (ADR 0181, round D): the core carries no `via:` of its own, and knows the release
train's by name only, so that a workflow naming it is refused naming the plugin that carries it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tranibor_core import carriers


@pytest.fixture
def no_carrier() -> Iterator[None]:
    saved = dict(carriers._CARRIERS)
    carriers._CARRIERS.clear()
    try:
        yield
    finally:
        carriers._CARRIERS.update(saved)


def test_the_core_carries_nothing_itself(no_carrier: None) -> None:
    assert carriers.known_carriers() == []
    why = carriers.unavailable_carrier("release_train")
    assert why is not None and "`software-delivery`" in why
    assert "no installed extension" in (carriers.unavailable_carrier("ferry") or "")


def test_a_declared_carrier_is_available_and_forgotten_with_its_plugin(no_carrier: None) -> None:
    carriers.declare_carrier("release_train", plugin="itsm")
    carriers.declare_carrier("release_train", plugin="itsm")  # again: nothing changes
    assert carriers.unavailable_carrier("release_train") is None
    carriers.forget("itsm")
    assert carriers.unavailable_carrier("release_train") is not None


def test_the_boarding_activity_is_the_history_s_name() -> None:
    assert carriers.BOARD_ACTIVITY == {"release_train": "signal_train"}
    assert set(carriers.PLUGIN_CARRIERS) == set(carriers.BOARD_ACTIVITY)
