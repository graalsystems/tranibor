"""Chart tests render the chart offline, as a plain `helm template` would.

The chart's default executor is `agent_sandbox` (ADR 0072), and it refuses to render when the
operator's CRD is not among the cluster's API versions. Argo CD and `helm install` hand those
versions over; an offline `helm template` does not. Every test of this directory therefore
renders as if the operator were installed, except a test marked `no_operator`, which renders
the way a cluster without it would.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

OPERATOR_API = "extensions.agents.x-k8s.io/v1beta1/SandboxTemplate"
NAME_LABEL = "app.kubernetes.io/name"


def names_selected(selector: dict[str, Any] | None, key: str = NAME_LABEL) -> set[str]:
    """The values of `key` a pod selector admits, from its `matchLabels` or an `In` expression: a policy
    of the chart selects one of the platform's pods under either value of its name (ADR 0226, R9b)."""
    selector = selector or {}
    found = {str(v) for k, v in (selector.get("matchLabels") or {}).items() if k == key}
    for expression in selector.get("matchExpressions") or []:
        if expression["key"] == key and expression["operator"] == "In":
            found |= {str(v) for v in expression["values"]}
    return found


def etc_mounts(container: dict[str, Any], volume: str) -> list[str]:
    """Where `volume` is mounted in `container`, and that every mount of it is read only: a file of the
    chart's under `/etc/tranibor/` is mounted at its former `/etc/choregos/` path too (ADR 0226, R9b)."""
    mounts = [m for m in container.get("volumeMounts", []) if m["name"] == volume]
    assert all(m.get("readOnly") is True for m in mounts), mounts
    return [str(m["mountPath"]) for m in mounts]


def both_paths(path: str) -> list[str]:
    """`/etc/tranibor/<x>` and `/etc/choregos/<x>`, in the order the chart mounts them."""
    return [path, "/etc/choregos/" + path.removeprefix("/etc/tranibor/")]


def both_names(*pods: str) -> set[str]:
    """`tranibor-<pod>` and `choregos-<pod>` for each pod: what a selector of the window admits."""
    return {f"{prefix}-{pod}" for pod in pods for prefix in ("tranibor", "choregos")}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "no_operator: render the chart as on a cluster without the agent-sandbox operator"
    )


@pytest.fixture(autouse=True)
def _operator_installed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("no_operator"):
        return
    real_run = subprocess.run

    def run(args: Any, *positional: Any, **keywords: Any) -> Any:
        if isinstance(args, list) and args[:2] == ["helm", "template"] and "--api-versions" not in args:
            args = [*args, "--api-versions", OPERATOR_API]
        return real_run(args, *positional, **keywords)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture(autouse=True)
def _chart_names_are_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The charts render `TRANIBOR_*` since ADR 0226 (R9b); a variable a values file still sets under its
    former name (`global.extraEnv`, `runner.env`) is adopted as `TRANIBOR_*` by a real process when it
    starts (ADR 0176 R4). A test that sets such a variable in the environment gets the same adoption."""
    set_env = monkeypatch.setenv

    def setenv(name: str, value: str, prepend: str | None = None) -> None:
        set_env(name, value, prepend)
        if name.startswith("CHOREGOS_"):  # compat-ok
            set_env("TRANIBOR_" + name.removeprefix("CHOREGOS_"), value)  # compat-ok

    monkeypatch.setenv = setenv  # type: ignore[method-assign]
