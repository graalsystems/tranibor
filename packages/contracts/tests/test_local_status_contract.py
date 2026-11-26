# SPDX-License-Identifier: Apache-2.0
"""What `choregos local status|doctor --json` prints (S87-08, ADR 0229 §2): one closed shape, the only
one the desktop app reads, held the same by the schema and the model.

Contract only: `choregos local` (S87-28) prints it and `packages/cli/tests/test_local_commands.py`
validates every `--json` output against the schema then; the desktop app's supervisor (S87-48)
replays fixtures that validate against it. Here, the schema and the model cannot drift apart.
"""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest
import tranibor_contracts as contracts
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from tranibor_contracts import local_status, relay
from tranibor_contracts.local_status import LocalStatus

from .test_contracts import _compare

EXAMPLES = contracts.schemas_dir() / "examples"
SCHEMA = contracts.load_schema("local-status.schema.json")
DEFS: dict[str, Any] = SCHEMA["$defs"]


def _json(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return loaded


def _validator() -> Draft202012Validator:
    return Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _both(document: Any) -> tuple[bool, bool]:
    """(the schema accepts it, the model accepts it)."""
    try:
        LocalStatus.model_validate(document)
    except ValidationError:
        model = False
    else:
        model = True
    return not list(_validator().iter_errors(document)), model


def _changed(document: dict[str, Any], path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    changed = json.loads(json.dumps(document))
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return changed


EXAMPLE_FILES = [
    "local-status.example.json",
    "local-status.first-launch.example.json",
    "local-status.relay-pairing.example.json",
]


@pytest.mark.parametrize("example", EXAMPLE_FILES)
def test_an_example_is_valid_for_the_schema_and_the_model(example: str) -> None:
    document = _json(example)
    errors = sorted(_validator().iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    parsed = LocalStatus.model_validate(document)
    dumped = parsed.model_dump(mode="json", exclude_none=True)
    assert set(document) - set(dumped) <= {key for key, value in document.items() if value is None}
    # What the model writes is what the schema accepts, its nulls included: `--json` prints the model.
    assert not list(_validator().iter_errors(parsed.model_dump(mode="json")))


def test_every_local_status_example_is_tested_and_the_schema_is_declared() -> None:
    assert {path.name for path in EXAMPLES.glob("local-status*")} == set(EXAMPLE_FILES)
    assert "local-status.schema.json" in contracts.SCHEMA_FILES


SAME_FIELDS: list[tuple[str, dict[str, Any], type[BaseModel]]] = [
    ("status", SCHEMA, LocalStatus),
    ("stack", DEFS["stack"], local_status.LocalStack),
    ("service", DEFS["service"], local_status.LocalService),
    ("runner", DEFS["runner"], local_status.LocalRunner),
    ("relay", DEFS["relay"], local_status.LocalRelay),
    ("pairing", DEFS["pairing"], local_status.LocalRelayPairing),
    ("backups", DEFS["backups"], local_status.LocalBackups),
    ("check", DEFS["check"], local_status.LocalCheck),
]


@pytest.mark.parametrize(("trail", "node", "model"), SAME_FIELDS, ids=[s[0] for s in SAME_FIELDS])
def test_a_schema_node_and_its_model_declare_the_same_fields(
    trail: str, node: dict[str, Any], model: type[BaseModel]
) -> None:
    gaps: list[str] = []
    _compare(trail, node, model, gaps)
    assert not gaps, "\n".join(gaps)
    assert node["additionalProperties"] is False, f"{trail} is closed"
    assert model.model_config.get("extra") == "forbid", f"{model.__name__} is closed"


def test_every_local_status_model_is_compared() -> None:
    models = {
        value
        for value in vars(local_status).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == local_status.__name__
    }
    assert models == {model for _, _, model in SAME_FIELDS}


@pytest.mark.parametrize(
    ("node", "literal"),
    [
        (DEFS["stack"]["properties"]["state"], local_status.StackState),
        (DEFS["service"]["properties"]["state"], local_status.ServiceState),
        (DEFS["runner"]["properties"]["state"], local_status.RunnerState),
    ],
)
def test_the_states_are_the_same_closed_list_in_the_schema_and_the_model(
    node: dict[str, Any], literal: Any
) -> None:
    assert node["enum"] == list(get_args(literal))


def test_a_field_the_shape_does_not_describe_is_refused_at_every_level() -> None:
    """The app and the CLI are of one release (ADR 0229, "Unchanged, on purpose"): a field the app
    does not know is a bug of the CLI, not a later need."""
    status = _json("local-status.example.json")
    for path in (
        ("later",),
        ("stack", "later"),
        ("services", 0, "later"),
        ("runner", "later"),
        ("relay", "later"),
        ("backups", "later"),
        ("checks", 0, "later"),
    ):
        assert _both(_changed(status, path, 1)) == (False, False), path


def test_a_runner_announces_its_providers_by_name_and_never_a_credential() -> None:
    """ADR 0227: the subscription's token lives with the runner; what the status says is a name."""
    status = _json("local-status.example.json")
    for name in ("claude", "codex", "a-provider"):
        assert _both(_changed(status, ("runner", "providers"), [name])) == (True, True), name
    for credential in (
        "sk-ant-oat01-" + "A" * 40,
        "Bearer abc",
        "claude:sk-ant-api03-xyz",
        "x" * 33,
        "Claude",
    ):
        assert _both(_changed(status, ("runner", "providers"), [credential])) == (False, False), credential
    assert _both(_changed(status, ("runner", "providers"), ["claude", "claude"])) == (False, False)
    assert set(DEFS["runner"]["properties"]) == {"state", "version", "providers"}


def test_a_pairing_shows_the_user_code_and_never_the_device_code() -> None:
    pairing = _json("local-status.relay-pairing.example.json")
    assert set(DEFS["pairing"]["properties"]) == {"user_code", "verification_uri", "expires_at"}
    secret = _changed(
        pairing, ("relay", "pairing", "device_code"), "Gm4cJx0aT9qVb2Rk7LsNw1EyZp5Hd8Ui3Fo6Ct0Xj2Q"
    )
    assert _both(secret) == (False, False)
    assert DEFS["pairing"]["properties"]["user_code"]["pattern"] == relay.USER_CODE_PATTERN
    assert DEFS["pairing"]["properties"]["verification_uri"]["pattern"] == relay.VERIFICATION_URI_PATTERN


@pytest.mark.parametrize(
    ("what", "value", "accepted"),
    [
        ("off", {"enabled": False}, True),
        (
            "being paired",
            {
                "enabled": False,
                "pairing": _json("local-status.relay-pairing.example.json")["relay"]["pairing"],
            },
            True,
        ),
        ("on", {"enabled": True, "hostname": "kq7mz3xw2pa5.relay.example"}, True),
        ("on without a hostname", {"enabled": True}, False),
        (
            "on while being paired",
            {
                "enabled": True,
                "hostname": "kq7mz3xw2pa5.relay.example",
                "pairing": {
                    "user_code": "BCDF-KMPT",
                    "verification_uri": "https://relay.example/pair",
                    "expires_at": "2026-11-26T09:51:02Z",
                },
            },
            False,
        ),
        ("off with a hostname", {"enabled": False, "hostname": "kq7mz3xw2pa5.relay.example"}, False),
        ("a hostname whose id is not one", {"enabled": True, "hostname": "my-laptop.relay.example"}, False),
    ],
)
def test_the_relay_says_off_being_paired_or_on_under_its_hostname(
    what: str, value: dict[str, Any], accepted: bool
) -> None:
    status = _changed(_json("local-status.example.json"), ("relay",), value)
    assert _both(status) == (accepted, accepted), what


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("http://127.0.0.1:8484/auth/owner#ticket=abc", True),
        ("https://localhost:8443/", True),
        ("http://[::1]:8484", True),
        ("https://kq7mz3xw2pa5.relay.example/", False),
        ("http://127.0.0.1.attacker.example/", False),
        ("http://192.168.1.20:8484/", False),
        ("javascript:alert(1)", False),
    ],
)
def test_a_console_url_is_on_the_loopback_door(url: str, accepted: bool) -> None:
    """ADR 0229 §7: the console window stays on the loopback origin; the URL `open --print-url`
    returns, ticket and all, is never another origin's."""
    status = _json("local-status.example.json")
    assert _both(status | {"open_url": url}) == (accepted, accepted), url
    assert _both(_changed(status, ("stack", "url"), url)) == (accepted, accepted), url


def test_a_service_and_a_check_are_listed_once() -> None:
    """The schema cannot say a name is unique among objects; the model, which `--json` prints, does."""
    status = _json("local-status.example.json")
    twice = status | {"services": [*status["services"], status["services"][0]]}
    assert not _both(twice)[1]
    twice = status | {"checks": [*status["checks"], status["checks"][0]]}
    assert not _both(twice)[1]
