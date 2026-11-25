"""The wire names and their former twins (ADR 0226, R7): read both, write the new one."""

from __future__ import annotations

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
