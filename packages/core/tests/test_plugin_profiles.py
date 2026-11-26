# SPDX-License-Identifier: Apache-2.0
"""Profiles a plugin declares and the servers that implement them (ADR 0224, S86-02).

A conforming fixture loads; each rule of §2 has a fixture that fails on it alone, with a message that names
the tool and the reason."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from tranibor_core.plugins import (
    PluginError,
    conformance_problems,
    foreign_profile_names,
    load_directory,
    profiles_from,
)

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "profiles"
NOTES, SHELF = FIXTURES / "notes", FIXTURES / "shelf"


def _edit(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _op(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(op for op in doc["operations"] if op["name"] == name)


@pytest.fixture
def shelf(tmp_path: Path) -> Path:
    copy = tmp_path / "shelf"
    shutil.copytree(SHELF, copy)
    return copy


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    copy = tmp_path / "notes"
    shutil.copytree(NOTES, copy)
    return copy


def _server(shelf: Path, change: Callable[[dict[str, Any]], None]) -> None:
    _edit(shelf / "servers" / "shelf.yaml", change)


def _against(shelf: Path, notes: Path = NOTES) -> str:
    return "\n".join(conformance_problems(load_directory(shelf), profiles_from([notes])))


def test_a_plugin_declares_its_profile_and_a_server_that_keeps_it_conforms() -> None:
    declared = load_directory(NOTES)
    assert list(declared.profiles) == ["notes/notes@1"]
    assert declared.manifest["contents"]["profiles"] == ["profiles/notes.yaml"]
    assert "profiles/notes.yaml" in declared.files  # the digest covers it, so does the archive
    implementer = load_directory(SHELF)
    assert foreign_profile_names(implementer) == ["notes/notes@1"]
    assert conformance_problems(implementer, profiles_from([NOTES])) == []
    assert conformance_problems(implementer, profiles_from([FIXTURES])) == []  # a folder of plugins


def test_the_digest_moves_with_a_profile(notes: Path) -> None:
    before = load_directory(notes).digest
    _edit(
        notes / "profiles" / "notes.yaml",
        lambda d: d["tools"][1]["input"]["properties"]["limit"].update(maximum=100),
    )
    assert load_directory(notes).digest != before


# -- the rules of §2, one fixture each ---------------------------------------------------------------------


def test_a_tool_of_the_profile_the_server_does_not_declare(shelf: Path) -> None:
    _server(shelf, lambda d: d["operations"].remove(_op(d, "list_notes")))
    assert "`list_notes` of `notes/notes@1` is not declared" in _against(shelf)


def test_a_tool_declared_with_another_access(shelf: Path) -> None:
    _server(shelf, lambda d: _op(d, "list_notes").update(access="write"))
    assert "`list_notes` of `notes/notes@1` is a read of the profile, declared `write`" in _against(shelf)


def test_a_floor_of_human_approval_is_never_lowered(shelf: Path, notes: Path) -> None:
    _edit(notes / "profiles" / "notes.yaml", lambda d: d["tools"][2].update(human_approval=True))
    _server(shelf, lambda d: _op(d, "add_note").update(human_approval=False))
    assert "`add_note` of `notes/notes@1` requires `human_approval: true`" in _against(shelf, notes)
    _server(shelf, lambda d: _op(d, "add_note").pop("human_approval"))
    assert "requires `human_approval: true`" in _against(shelf, notes)


def test_governance_may_be_raised(shelf: Path) -> None:
    # The fixture's server asks approval where the profile says `false`: allowed.
    assert (
        _op(yaml.safe_load((shelf / "servers" / "shelf.yaml").read_text()), "add_note")["human_approval"]
        is True
    )
    assert _against(shelf) == ""


def test_a_platform_argument_is_never_removed(shelf: Path) -> None:
    _server(shelf, lambda d: _op(d, "add_note").update(platform_arguments=[]))
    assert "must keep the platform arguments ['action_key']" in _against(shelf)


def test_a_server_input_that_refuses_what_the_profile_accepts(shelf: Path) -> None:
    def stricter(doc: dict[str, Any]) -> None:
        _op(doc, "list_notes")["input_schema"]["properties"]["limit"]["maximum"] = 100  # profile: 200

    _server(shelf, stricter)
    found = _against(shelf)
    assert "`list_notes` of `notes/notes@1`, input does not accept what the profile accepts" in found
    assert "/limit: `maximum` is 100 for the server but 200 for the profile" in found


def test_a_server_input_that_misses_a_property_of_the_profile(shelf: Path) -> None:
    def missing(doc: dict[str, Any]) -> None:
        del _op(doc, "list_notes")["input_schema"]["properties"]["tag"]

    _server(shelf, missing)
    assert "the property `tag` is allowed by the profile and refused by the server" in _against(shelf)


def test_a_server_input_that_requires_what_the_profile_does_not(shelf: Path) -> None:
    def requires(doc: dict[str, Any]) -> None:
        _op(doc, "list_notes")["input_schema"]["required"] = ["tag"]

    _server(shelf, requires)
    # Required on the server side: the profile's instances without `tag` are refused.
    assert "`tag` is required by the server and optional for the profile" in _against(shelf)


def test_a_server_output_the_profile_does_not_accept(shelf: Path) -> None:
    def widen(doc: dict[str, Any]) -> None:
        _op(doc, "get_provider")["output_schema"]["properties"]["provider"] = {"type": ["string", "integer"]}

    _server(shelf, widen)
    found = _against(shelf)
    assert "`get_provider` of `notes/notes@1`, output returns what the profile does not accept" in found
    assert "type `integer`" in found


def test_a_server_output_that_leaves_out_a_required_field(shelf: Path) -> None:
    def leave_out(doc: dict[str, Any]) -> None:
        _op(doc, "list_notes")["output_schema"]["required"] = []

    _server(shelf, leave_out)
    assert "`notes` is required by the profile and optional for the server" in _against(shelf)


def test_a_definition_is_followed_through_a_ref(shelf: Path) -> None:
    def break_note(doc: dict[str, Any]) -> None:
        note = _op(doc, "list_notes")["output_schema"]["$defs"]["note"]
        note["properties"]["created"] = {"type": "string"}  # the profile says date-time

    _server(shelf, break_note)
    assert "`format` is 'date-time' for the profile but None for the server" in _against(shelf)


def test_a_server_that_declares_no_schema_cannot_be_compared(shelf: Path) -> None:
    _server(shelf, lambda d: _op(d, "list_notes").pop("output_schema"))
    assert "declares no `output_schema`" in _against(shelf)
    _server(shelf, lambda d: _op(d, "list_notes").pop("input_schema"))
    assert "declares no `input_schema`" in _against(shelf)


def test_a_server_keyword_outside_the_subset_fails_rather_than_passes(shelf: Path) -> None:
    def clever(doc: dict[str, Any]) -> None:
        _op(doc, "list_notes")["input_schema"]["properties"]["tag"] = {"anyOf": [{"type": "string"}]}

    _server(shelf, clever)
    assert "anyOf" in _against(shelf)


def test_the_profile_plugin_must_be_required(shelf: Path) -> None:
    _edit(shelf / "plugin.yaml", lambda d: d["requires"].update(plugins=[]))
    with pytest.raises(PluginError) as caught:
        load_directory(shelf)  # without any profile document in hand: a rule of the manifest
    assert "`notes` is not in this plugin's `requires.plugins`" in "\n".join(caught.value.problems)


def test_a_profile_the_plugin_does_not_declare(shelf: Path) -> None:
    _server(shelf, lambda d: d.update(implements=["notes/notes@2"]))
    assert "implements `notes/notes@2`, which notes does not declare" in _against(shelf)


def test_the_plugin_that_declares_a_profile_may_implement_it_itself(notes: Path) -> None:
    shelf = SHELF / "servers" / "shelf.yaml"
    (notes / "servers").mkdir()
    shutil.copy(shelf, notes / "servers" / "shelf.yaml")
    _edit(notes / "plugin.yaml", lambda d: d["contents"].update(servers=["servers/shelf.yaml"]))
    assert load_directory(notes).servers["shelf"]["implements"] == ["notes/notes@1"]
    _server(notes, lambda d: _op(d, "list_notes").update(access="write"))
    with pytest.raises(PluginError) as caught:
        load_directory(notes)  # its own profile is known to the loader: no other plugin to hand in
    assert "`list_notes` of `notes/notes@1` is a read of the profile" in "\n".join(caught.value.problems)


def test_without_the_other_plugins_profiles_nothing_is_said_to_pass(shelf: Path) -> None:
    plugin = load_directory(shelf)  # loads: its own rules hold
    assert conformance_problems(plugin) == []  # unchecked, and `foreign_profile_names` says what
    assert foreign_profile_names(plugin) == ["notes/notes@1"]


# -- a profile file ----------------------------------------------------------------------------------------


def _profile(notes: Path, change: Callable[[dict[str, Any]], None]) -> str:
    _edit(notes / "profiles" / "notes.yaml", change)
    with pytest.raises(PluginError) as caught:
        load_directory(notes)
    return "\n".join(caught.value.problems)


def test_a_profile_outside_the_subset_is_refused(notes: Path) -> None:
    found = _profile(notes, lambda d: d["tools"][1]["output"].update(oneOf=[{"type": "string"}]))
    assert "profiles/notes.yaml" in found and "oneOf" in found


def test_a_profile_ref_that_names_nothing_is_refused(notes: Path) -> None:
    found = _profile(notes, lambda d: d["tools"][2].update(output={"$ref": "#/$defs/missing"}))
    assert "`$ref` '#/$defs/missing' names no `#/$defs/<name>`" in found


def test_a_profile_definition_that_reaches_itself_is_refused(notes: Path) -> None:
    def cycle(doc: dict[str, Any]) -> None:
        doc["$defs"]["tree"] = {"type": "object", "properties": {"child": {"$ref": "#/$defs/tree"}}}

    assert "reaches itself through `$ref`" in _profile(notes, cycle)


def test_a_tool_declared_twice_is_refused(notes: Path) -> None:
    found = _profile(notes, lambda d: d["tools"].append(dict(d["tools"][0])))
    assert "tool `get_provider` is declared twice" in found


def test_a_profile_declared_twice_is_refused(notes: Path) -> None:
    shutil.copy(notes / "profiles" / "notes.yaml", notes / "profiles" / "again.yaml")
    _edit(
        notes / "plugin.yaml",
        lambda d: d["contents"].update(profiles=["profiles/notes.yaml", "profiles/again.yaml"]),
    )
    with pytest.raises(PluginError) as caught:
        load_directory(notes)
    assert "profile `notes/notes@1` is declared twice" in "\n".join(caught.value.problems)


def test_a_profile_file_that_is_missing_is_refused(notes: Path) -> None:
    (notes / "profiles" / "notes.yaml").unlink()
    with pytest.raises(PluginError) as caught:
        load_directory(notes)
    assert "profiles/notes.yaml: profile file missing" in "\n".join(caught.value.problems)
