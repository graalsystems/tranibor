"""The release train's Prometheus series are the plugin's, declared to `/metrics` through
`metrics.declare_series` (ADR 0181, round D).

`tranibor_releases_total`, `tranibor_train_frozen` and `tranibor_train_status` left the core's inventory:
the extension declares them and refreshes them with the snapshot; without it `/metrics` does not emit them,
and the core knows them by name only (`PLUGIN_SERIES`), so the chart's dashboards still cite them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tranibor_api import metrics
from tranibor_software_delivery import NAME
from tranibor_software_delivery.extension import register
from tranibor_software_delivery.train import metrics as train_metrics

SERIES = set(train_metrics.NAMES)


@pytest.fixture
def without_the_extension() -> Iterator[None]:
    metrics.forget_series(NAME)
    try:
        yield
    finally:
        register()


def test_the_extension_declares_the_train_s_series_and_the_core_emits_none_of_them() -> None:
    assert metrics.emitted_names() >= SERIES
    assert not SERIES & metrics.NAMES
    assert dict.fromkeys(SERIES, NAME) == {k: v for k, v in metrics.PLUGIN_SERIES.items() if k in SERIES}


def test_without_the_extension_the_series_are_not_emitted(without_the_extension: None) -> None:
    assert not SERIES & metrics.emitted_names()


async def test_a_refresh_renders_the_three_families(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_trains(slugs: list[str]) -> tuple[dict[tuple[str, str], float], ...]:
        return {("billing", "prod"): 1.0}, {("billing", "prod"): 2.0}

    monkeypatch.setattr(train_metrics, "_trains", no_trains)

    class _Rows:
        def all(self) -> list[tuple[str, int]]:
            return [("done", 3)]

    class _Session:
        async def execute(self, _statement: object) -> _Rows:
            return _Rows()

    class _Scope:
        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(train_metrics, "session_scope", lambda **_: _Scope())
    families = {f.name: f for f in await train_metrics.refresh(["billing"])}
    assert set(families) == {"tranibor_releases_total", "tranibor_train_frozen", "tranibor_train_status"}
    assert [s.value for s in families["tranibor_train_status"].samples] == [2.0]


def test_a_series_the_core_emits_or_another_plugin_declared_is_refused() -> None:
    async def none(slugs: list[str]) -> list[object]:
        return []

    with pytest.raises(ValueError, match="core already emits"):
        metrics.declare_series("itsm", ["tranibor_runs_active"], none)
    with pytest.raises(ValueError, match="already emitted by the plugin"):
        metrics.declare_series("itsm", ["tranibor_train_status"], none)
    # A series is served under its former name too (ADR 0226, R9b): the twin is claimed with it.
    with pytest.raises(ValueError, match="core already emits"):
        metrics.declare_series("itsm", ["choregos_runs_active"], none)
    with pytest.raises(ValueError, match="already emitted by the plugin"):
        metrics.declare_series("itsm", ["choregos_train_status"], none)
