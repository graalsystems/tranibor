"""The runbooks and the host pages say what the chart renders (S80-43, ADR 0195).

Three sentences were wrong after the generic deployment landed: the Temporal backup runbook knew only the
SQLite bench, the docs site said every embedded dependency is refused in a serious environment, and the
Ecphoria quorum runbook described a cluster the chart does not render. Each is checked against the guard
or the template it describes.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_the_temporal_backup_runbook_covers_server_mode() -> None:
    assert 'ne (toString ((index .Values "choregos-temporal"' in _read("charts/choregos/templates/guard.yaml")
    runbook = _read("docs/runbooks/temporal-backup.md")
    assert "`server` mode" in runbook and "temporal_visibility" in runbook


def test_the_site_does_not_say_every_embedded_dependency_is_refused() -> None:
    install = _read("docs/site/host/install.md")
    assert "refuses to render with an embedded dependency" not in install
    assert "`server`\n  mode" in install or "`server` mode" in install
    assert "bench" in _read("docs/site/host/index.md") and "`server` mode" in _read("docs/site/host/index.md")


def test_the_quorum_runbook_says_the_charts_ecphoria_has_one_replica() -> None:
    assert "replicas: 1" in _read("charts/choregos/charts/ecphoria/templates/deployment.yaml")
    assert "one replica on one volume" in _read("docs/runbooks/ecphoria-quorum.md")


def test_the_site_lists_the_memory_among_what_to_back_up() -> None:
    assert "Ecphoria's own admin API" in _read("docs/site/host/backups.md")
