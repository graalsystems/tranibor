# SPDX-License-Identifier: Apache-2.0
"""What `/metrics` can produce, and the series an extension declares beside the core's (ADR 0181, round D).

Standard library only: an extension declares its series when it loads (`declare_series`), in every process
that loads the extensions, including the outposts' door served alone, which must not import the API's
`metrics` module (Prometheus, the database). `tranibor_api.metrics` reads these, and serves them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The inventory: what `/metrics` can produce. The dashboards test refers to it.
NAMES = frozenset(
    {
        "http_requests_total",
        "choregos_runs_active",
        "choregos_runs_total",
        "choregos_run_start_seconds",
        "choregos_cost_usd_total",
        "choregos_project_daily_cost_usd",
        "choregos_project_daily_budget_usd",
        "choregos_first_pass_merge_rate",
        "choregos_lead_time_seconds",
        "choregos_permission_denials_total",
        "choregos_memory_pending",
        "choregos_context_pack_empty_total",
        "choregos_edition_info",
        "choregos_outpost_poll_conflicts_total",
        "choregos_audit_export_events_total",
        "choregos_audit_export_failures_total",
        "choregos_audit_export_lag_entries",
        "choregos_audit_export_progress_timestamp_seconds",
        "choregos_audit_retention_pruned_entries_total",
        "choregos_audit_retention_held_back_total",
        "choregos_audit_retention_failures_total",
    }
)

#: The series a plugin's extension emits, by name, and the plugin (ADR 0181, round D): what the core can
#: say of a series a dashboard cites when the extension is not installed, as `gates.PLUGIN_GATES` does for
#: gates. The release train's three are the `software-delivery` plugin's.
PLUGIN_SERIES = {
    "choregos_releases_total": "software-delivery",
    "choregos_train_frozen": "software-delivery",
    "choregos_train_status": "software-delivery",
}

#: (plugin, names, refresh): what extensions declared through `declare_series`.
_SERIES: list[tuple[str, frozenset[str], Any]] = []


def declare_series(plugin: str, names: Iterable[str], refresh: Any) -> None:
    """Called by an extension when it loads: `refresh(project_slugs)` (async) returns the metric families
    of `names`, computed at each refresh of the snapshot and served on `/metrics` beside the core's. A name
    the core emits, or another extension declared, stops the start. Idempotent for the same plugin."""
    declared = frozenset(names)
    if not declared or not callable(refresh):
        raise TypeError("a plugin's series are names and an async refresh(project_slugs) -> families")
    if declared & NAMES:
        raise ValueError(f"the core already emits {sorted(declared & NAMES)}")
    for other, known, _ in list(_SERIES):
        if other == plugin:
            _SERIES.remove((other, known, _))
        elif declared & known:
            raise ValueError(f"{sorted(declared & known)} already emitted by the plugin `{other}`")
    _SERIES.append((plugin, declared, refresh))


def forget_series(plugin: str) -> None:
    """Drop what `plugin` declared (a test that installed an extension does it after)."""
    _SERIES[:] = [entry for entry in _SERIES if entry[0] != plugin]


def emitted_names() -> frozenset[str]:
    """What `/metrics` produces in this deployment: the core's series and the installed extensions'."""
    return NAMES.union(*(names for _, names, _ in _SERIES))


__all__ = ["NAMES", "PLUGIN_SERIES", "declare_series", "emitted_names", "forget_series"]
