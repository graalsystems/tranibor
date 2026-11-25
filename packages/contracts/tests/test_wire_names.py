"""The wire names and their former twins (ADR 0226, R7): read both, write the new one."""

from __future__ import annotations

import pathlib

import pytest
from tranibor_contracts import wire


def test_a_header_is_read_under_its_new_name_first_then_its_former_one() -> None:
    assert wire.header("Secret") == "X-Tranibor-Secret"
    assert wire.former_header("Secret") == "X-Choregos-Secret"
    assert wire.read_header({"x-tranibor-secret": "new", "x-choregos-secret": "old"}, "Secret") == "new"
    assert wire.read_header({"X-Choregos-Secret": "old"}, "Secret") == "old"
    assert wire.read_header({"X-Choregos-Secret": "old", "X-Tranibor-Secret": ""}, "Secret") == "old"
    assert wire.read_header({}, "Secret") == ""
    assert wire.read_header({}, "Secret", "none") == "none"


def test_a_header_sent_to_a_reader_of_either_name_carries_both() -> None:
    assert wire.both_headers("Signature", "t=1,v1=ab") == {
        "X-Tranibor-Signature": "t=1,v1=ab",
        "X-Choregos-Signature": "t=1,v1=ab",
    }
    relayed = wire.with_twin_headers({"x-choregos-secret": "s", "X-Tranibor-Token": "t", "Accept": "*/*"})
    lowered = {k.lower(): v for k, v in relayed.items()}
    assert lowered == {
        "x-choregos-secret": "s",
        "x-tranibor-secret": "s",
        "x-tranibor-token": "t",
        "x-choregos-token": "t",
        "accept": "*/*",
    }
    # A twin the sender set itself keeps its own value.
    both = wire.with_twin_headers({"X-Tranibor-Secret": "a", "X-Choregos-Secret": "b"})
    assert both == {"X-Tranibor-Secret": "a", "X-Choregos-Secret": "b"}


def test_the_access_key_of_a_tool_is_read_under_either_name() -> None:
    assert wire.read_access({"x-tranibor-access": "write", "x-choregos-access": "read"}) == "write"
    assert wire.read_access({"x-choregos-access": "approval"}) == "approval"
    assert wire.read_access({}) is None


def test_both_api_versions_are_the_platform_s() -> None:
    assert wire.is_api_version("tranibor/v1") and wire.is_api_version("choregos/v1")
    assert not wire.is_api_version("v1")
    assert wire.is_kube_api_version("tranibor.dev/v1alpha1") and wire.is_kube_api_version(
        "choregos.dev/v1alpha1"
    )
    assert wire.API_VERSION == "tranibor/v1" and wire.KUBE_API_VERSION == "tranibor.dev/v1alpha1"


def test_labels_are_written_under_both_families_and_read_under_either() -> None:
    written = wire.labels_with_former({"tranibor.dev/org": "acme", "tranibor/run-id": "r1", "app": "x"})
    assert written == {
        "tranibor.dev/org": "acme",
        "choregos.dev/org": "acme",
        "tranibor/run-id": "r1",
        "choregos/run-id": "r1",
        "app": "x",
    }
    assert wire.read_label({"choregos.dev/org": "old"}, "org") == "old"
    assert wire.read_label({"tranibor.dev/org": "new", "choregos.dev/org": "old"}, "org") == "new"
    assert wire.read_label({"choregos/run-id": "r"}, "run-id", domain=False) == "r"
    assert wire.read_label(None, "org") is None


def test_a_group_is_namespaced_under_either_prefix() -> None:
    assert wire.group("acme", "architects") == "tranibor:acme:architects"
    assert wire.group_org_and_name("tranibor:acme:architects") == ("acme", "architects")
    assert wire.group_org_and_name("choregos:acme:architects") == ("acme", "architects")
    assert wire.group_org_and_name("architects") is None
    assert wire.group_org_and_name("tranibor:acme") is None
    assert wire.strip_group_prefix("choregos:acme:x") == "acme:x"


def test_a_cookie_is_read_under_its_new_name_then_its_former_one() -> None:
    assert wire.cookie("session") == "tranibor_session"
    assert wire.read_cookie({"tranibor_session": "n", "choregos_session": "o"}, "session") == ("n", False)
    assert wire.read_cookie({"choregos_session": "o"}, "session") == ("o", True)
    assert wire.read_cookie({}, "session") == (None, False)


def test_a_marker_is_written_new_and_found_under_either_name() -> None:
    assert wire.marker("origin") == "<!-- tranibor:origin -->"
    assert wire.marker("status", html=False) == "[tranibor:status]"
    assert wire.has_marker("x <!-- choregos:origin --> y", "origin")
    assert wire.has_marker("<!-- tranibor:origin -->", "origin")
    assert not wire.has_marker("<!-- tranibor:delivered -->", "origin")
    assert not wire.has_marker(None, "origin")
    assert wire.has_marker("[choregos:status] text", "status", html=False)


def test_the_platform_s_own_groups_are_emitted_under_both_prefixes_for_the_window() -> None:
    assert wire.group_names("acme", "architects") == ("tranibor:acme:architects", "choregos:acme:architects")


def test_the_schemas_accept_both_api_versions_and_name_themselves_tranibor() -> None:
    from tranibor_contracts import load_schema

    for name in ("workflow", "policy", "plugin", "plugin-index", "template", "assistant"):
        schema = load_schema(f"{name}.schema.json")
        assert schema["$id"].startswith(wire.SCHEMA_ID_BASE), name
        assert set(schema["properties"]["apiVersion"]["enum"]) == set(wire.API_VERSIONS), name


def test_a_whole_marker_has_two_forms_and_is_found_under_either() -> None:
    assert wire.marker_twins("<!-- tranibor:status -->") == (
        "<!-- tranibor:status -->",
        "<!-- choregos:status -->",
    )
    assert wire.marker_twins("[choregos:3fa2]") == ("[tranibor:3fa2]", "[choregos:3fa2]")
    assert wire.marker_twins("plain") == ("plain",)
    assert wire.carries_marker("x\n<!-- choregos:status -->\nold", "<!-- tranibor:status -->")
    assert not wire.carries_marker("<!-- tranibor:human -->", "<!-- tranibor:status -->")
    assert not wire.carries_marker(None, "<!-- tranibor:status -->")


# ─── round R9c: what the product shows, posts and loads (ADR 0226) ──────────────────────────────────


def test_the_a2a_block_is_written_under_both_keys_and_read_under_either() -> None:
    assert wire.a2a_metadata({"state": "inbox"}) == {
        "tranibor": {"state": "inbox"},
        "choregos": {"state": "inbox"},
    }
    assert wire.read_a2a_metadata({"tranibor": {"skill": "new"}, "choregos": {"skill": "old"}}) == {
        "skill": "new"
    }
    assert wire.read_a2a_metadata({"choregos": {"skill": "old"}}) == {"skill": "old"}
    assert wire.read_a2a_metadata({"tranibor": "not a block", "choregos": {"skill": "old"}}) == {
        "skill": "old"
    }
    assert wire.read_a2a_metadata(None) == {}


def test_a_state_label_is_written_new_and_recognised_under_either_prefix() -> None:
    assert wire.state_label("inbox") == "tranibor:inbox"
    assert wire.is_state_label("tranibor:inbox") and wire.is_state_label("choregos:inbox")
    assert wire.is_state_label("choregos::implement") and wire.is_state_label("tranibor::implement")
    assert not wire.is_state_label("agent-ready") and not wire.is_state_label("source:tranibor")
    assert wire.state_label_twins("choregos::implement") == ("tranibor::implement", "choregos::implement")
    assert wire.state_label_twins("tranibor:inbox") == ("tranibor:inbox", "choregos:inbox")
    assert wire.state_label_twins("agent-ready") == ("agent-ready",)


def test_a_check_is_posted_under_both_names_and_a_chat_button_read_under_either() -> None:
    assert wire.check_names("scope") == ("tranibor/scope", "choregos/scope")
    assert wire.chat_action("approve") == "tranibor:approve"
    assert wire.chat_action_name("tranibor:approve") == "approve"
    assert wire.chat_action_name("choregos:open") == "open"
    assert wire.chat_action_name("someone-else") is None
    assert wire.MCP_CLIENT_NAME == "tranibor"
    assert wire.COMMAND_WORDS == ("tranibor", "choregos")


def test_an_event_type_reads_as_its_new_name_and_is_queried_under_both() -> None:
    assert wire.event_type("choregos.run.finished") == "tranibor.run.finished"
    assert wire.event_type("tranibor.run.finished") == "tranibor.run.finished"
    assert wire.event_type("run.result") == "run.result"
    assert wire.event_type_twins("choregos.release.frozen") == (
        "tranibor.release.frozen",
        "choregos.release.frozen",
    )
    assert wire.event_type_twins("tranibor.run.finished") == (
        "tranibor.run.finished",
        "choregos.run.finished",
    )


def test_both_entry_point_groups_are_read_and_a_name_registered_under_both_loads_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert wire.entry_point_groups("tranibor.plugins") == ("tranibor.plugins", "choregos.plugins")
    assert wire.entry_point_groups("choregos.migrations") == ("tranibor.migrations", "choregos.migrations")
    for name, groups in {
        "r9c_new": ["tranibor.r9c"],
        "r9c_former": ["choregos.r9c"],
        "r9c_both": ["tranibor.r9c", "choregos.r9c"],
    }.items():
        info = tmp_path / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8"
        )
        sections = "".join(f"[{group}]\n{name} = {name}:{group.split('.')[0]}\n" for group in groups)
        (info / "entry_points.txt").write_text(sections, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = wire.entry_points("tranibor.r9c")
    assert sorted(p.name for p in found) == ["r9c_both", "r9c_former", "r9c_new"], "each name once"
    assert next(p for p in found if p.name == "r9c_both").value == "r9c_both:tranibor", "the new group wins"
    assert [p.name for p in wire.entry_points("choregos.r9c")] == [p.name for p in found]


@pytest.mark.parametrize("written", ["tranibor.run.finished", "choregos.run.finished"], ids=["new", "former"])
def test_an_event_read_back_under_either_type_is_the_core_s_member(written: str) -> None:
    """An `events` row or a notification from a replica of the previous release carries the former type."""
    from tranibor_contracts import EVENT_TYPES, ChoregosEvent, EventType

    assert EVENT_TYPES.check(written) is EventType.RUN_FINISHED
    event = ChoregosEvent.model_validate({"source": "/choregos/api", "type": written, "data": {}})
    assert event.type == EventType.RUN_FINISHED == "tranibor.run.finished"
    assert ChoregosEvent.emit(EventType.RUN_FINISHED).type == "tranibor.run.finished", "emitted new only"
