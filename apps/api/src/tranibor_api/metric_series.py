# SPDX-License-Identifier: Apache-2.0
"""What `/metrics` can produce, and the series an extension declares beside the core's (ADR 0181, round D).

Standard library only: an extension declares its series when it loads (`declare_series`), in every process
that loads the extensions, including the outposts' door served alone, which must not import the API's
`metrics` module (Prometheus, the database). `tranibor_api.metrics` reads these, and serves them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The prefix of the platform's series (ADR 0226, R9b), and the former one. Every `tranibor_*` family is
#: served under its `choregos_*` name too, with the same samples, until 0.32.0 (`metrics.expose`): an
#: operator's own dashboards, recording rules and alerts read the former names, and a renamed series would
#: leave them empty without a word. The cost is the platform's own series counted twice in the time-series
#: database: a few dozen families, whose cardinality is the projects (times stages and models for the costs)
#: and the sinks, not the runs.
PREFIX = "tranibor_"
FORMER_PREFIX = "choregos_"


def former_name(name: str) -> str | None:
    """`choregos_x` for `tranibor_x`, `None` for a name the platform does not prefix."""
    return FORMER_PREFIX + name.removeprefix(PREFIX) if name.startswith(PREFIX) else None


#: The series the platform names, under their new names.
CURRENT_NAMES = frozenset(
    {
        "http_requests_total",
        "tranibor_runs_active",
        "tranibor_runs_total",
        "tranibor_run_start_seconds",
        "tranibor_cost_usd_total",
        "tranibor_project_daily_cost_usd",
        "tranibor_project_daily_budget_usd",
        "tranibor_first_pass_merge_rate",
        "tranibor_lead_time_seconds",
        "tranibor_permission_denials_total",
        "tranibor_memory_pending",
        "tranibor_context_pack_empty_total",
        "tranibor_edition_info",
        "tranibor_outpost_poll_conflicts_total",
        "tranibor_audit_export_events_total",
        "tranibor_audit_export_failures_total",
        "tranibor_audit_export_lag_entries",
        "tranibor_audit_export_progress_timestamp_seconds",
        "tranibor_audit_retention_pruned_entries_total",
        "tranibor_audit_retention_held_back_total",
        "tranibor_audit_retention_failures_total",
    }
)
#: The inventory: what `/metrics` can produce, the former twins included. The dashboards test refers to it.
NAMES = CURRENT_NAMES | {twin for name in CURRENT_NAMES if (twin := former_name(name))}

#: The series a plugin's extension emits, by name, and the plugin (ADR 0181, round D): what the core can
#: say of a series a dashboard cites when the extension is not installed, as `gates.PLUGIN_GATES` does for
#: gates. The release train's three are the `software-delivery` plugin's.
PLUGIN_SERIES = {
    name: "software-delivery"
    for current in ("tranibor_releases_total", "tranibor_train_frozen", "tranibor_train_status")
    for name in (current, former_name(current) or current)
}

#: (plugin, names, refresh): what extensions declared through `declare_series`.
_SERIES: list[tuple[str, frozenset[str], Any]] = []


def declare_series(plugin: str, names: Iterable[str], refresh: Any) -> None:
    """Called by an extension when it loads: `refresh(project_slugs)` (async) returns the metric families
    of `names`, computed at each refresh of the snapshot and served on `/metrics` beside the core's. A name
    the core emits, or another extension declared, stops the start. Idempotent for the same plugin."""
    named = frozenset(names)
    # Served under its former name too (`former_name`): the twin is claimed with it.
    declared = named | {twin for name in named if (twin := former_name(name))}
    if not named or not callable(refresh):
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


__all__ = [
    "CURRENT_NAMES",
    "FORMER_PREFIX",
    "NAMES",
    "PLUGIN_SERIES",
    "PREFIX",
    "declare_series",
    "emitted_names",
    "forget_series",
    "former_name",
]
