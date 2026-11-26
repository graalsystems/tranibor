"""The scope of a `read` workspace (ADR 0119) takes the run directory's new name (ADR 0226, R9a).

A run started from 0.30.0 records `.tranibor/outputs/**`; one recorded before records the former
`.choregos/outputs/**`, and is still a read-only run until 0.32.0: neither may widen its scope.
"""

from __future__ import annotations

from tranibor_core.workspace import READ_ONLY_WRITE_PATHS, is_read_only_scope, write_paths_for


def test_a_read_run_is_given_the_new_name() -> None:
    assert write_paths_for("read", ["src/**"]) == [".tranibor/outputs/**"] == list(READ_ONLY_WRITE_PATHS)
    assert is_read_only_scope([".tranibor/outputs/**"])


def test_a_read_run_recorded_before_0_30_is_still_read_only() -> None:
    assert is_read_only_scope([".choregos/outputs/**"])


def test_any_other_scope_is_not_read_only() -> None:
    for scope in ([], None, ["**"], ["src/**"], [".tranibor/outputs/**", "src/**"], [".tranibor/**"]):
        assert not is_read_only_scope(scope)
    assert write_paths_for("write", []) == ["**"]
