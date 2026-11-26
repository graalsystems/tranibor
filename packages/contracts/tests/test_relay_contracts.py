# SPDX-License-Identifier: Apache-2.0
"""The relay (S87-08, ADR 0230): the schemas, the models and `relay.yaml` say the same thing.

Contract only: no relay serves any of this yet (S87-42 builds the server, S87-43 the agent), so what
is proven here is what the documents say, and that the three forms a relay and an agent will read
(JSON Schema, Python model, the relay's own document) cannot drift apart. The guarantees the ADR
puts on the relay's behaviour are held by `packages/sni-relay/tests/` once it exists.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import pytest
import tranibor_contracts as contracts
import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from tranibor_contracts import outpost, relay

from .test_contracts import _compare

EXAMPLES = contracts.schemas_dir() / "examples"
DOCUMENT = contracts.load_relay()
COMPONENTS: dict[str, Any] = DOCUMENT["components"]["schemas"]
VERBS = {"get", "post", "put", "patch", "delete"}

#: The five schemas ADR 0230 §2 names, each closed to extra fields.
FIVE = (
    "relay-hello.schema.json",
    "relay-open-stream.schema.json",
    "relay-pairing-request.schema.json",
    "relay-pairing-answer.schema.json",
    "relay-metadata-line.schema.json",
)


def _json(name: str) -> Any:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _file(schema: str) -> Draft202012Validator:
    return Draft202012Validator(
        contracts.load_schema(schema), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def _component(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"components": DOCUMENT["components"], **COMPONENTS[name]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _accepts(model: type[BaseModel], document: Any) -> bool:
    try:
        model.model_validate(document)
    except ValidationError:
        return False
    return True


def _both(schema: str, model: type[BaseModel], document: Any) -> tuple[bool, bool]:
    """(the schema accepts it, the model accepts it)."""
    return not list(_file(schema).iter_errors(document)), _accepts(model, document)


# ───────────────────────── every example, against its schema and its model ─────────────────────────

#: (example, schema file, model). A `.jsonl` example is read line by line.
FILE_EXAMPLES: list[tuple[str, str, type[BaseModel]]] = [
    ("relay-pairing-request.example.json", "relay-pairing-request.schema.json", relay.RelayPairingRequest),
    ("relay-pairing-answer.example.json", "relay-pairing-answer.schema.json", relay.RelayPairingAnswer),
    (
        "relay-pairing-answer.approved.example.json",
        "relay-pairing-answer.schema.json",
        relay.RelayPairingAnswer,
    ),
    ("relay-hello.example.json", "relay-hello.schema.json", relay.RelayHello),
    ("relay-open-stream.example.json", "relay-open-stream.schema.json", relay.RelayOpenStream),
    ("relay-metadata-line.example.json", "relay-metadata-line.schema.json", relay.RelayMetadataLine),
    ("relay-metadata-log.example.jsonl", "relay-metadata-line.schema.json", relay.RelayMetadataLine),
]

#: (example, component of `relay.yaml`, model).
DOCUMENT_EXAMPLES: list[tuple[str, str, type[BaseModel]]] = [
    ("relay-pairing-poll.example.json", "PairingPoll", relay.RelayPairingPoll),
    ("relay-challenge.example.json", "Challenge", relay.RelayChallenge),
    ("relay-welcome.example.json", "Welcome", relay.RelayWelcome),
    ("relay-close.example.json", "Close", relay.RelayClose),
    ("relay-problem.example.json", "RelayProblem", relay.RelayProblem),
]


def _documents(example: str) -> list[Any]:
    if example.endswith(".jsonl"):
        lines = (EXAMPLES / example).read_text(encoding="utf-8").splitlines()
        assert len(lines) > 1, "a log of one line shows nothing a single line does not"
        return [json.loads(line) for line in lines]
    return [_json(example)]


def _keeps_every_key(model: type[BaseModel], document: dict[str, Any]) -> None:
    """A model that validates an example and drops one of its keys has drifted from the schema."""
    dumped = model.model_validate(document).model_dump(mode="json", exclude_none=True)
    lost = {key for key, value in document.items() if value is not None} - set(dumped)
    assert not lost, f"keys lost by {model.__name__}: {sorted(lost)}"


@pytest.mark.parametrize(("example", "schema", "model"), FILE_EXAMPLES)
def test_an_example_is_valid_for_its_schema_and_its_model(
    example: str, schema: str, model: type[BaseModel]
) -> None:
    for document in _documents(example):
        errors = sorted(_file(schema).iter_errors(document), key=lambda e: list(e.path))
        assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
        _keeps_every_key(model, document)
        # What a model writes back is what the schema accepts, its nulls included.
        written = model.model_validate(document).model_dump(mode="json")
        assert not list(_file(schema).iter_errors(written))


@pytest.mark.parametrize(("example", "component", "model"), DOCUMENT_EXAMPLES)
def test_an_example_of_the_document_is_valid_for_its_component_and_its_model(
    example: str, component: str, model: type[BaseModel]
) -> None:
    document = _json(example)
    errors = sorted(_component(component).iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    _keeps_every_key(model, document)


def test_no_relay_example_is_left_untested() -> None:
    """An example nobody validates says nothing."""
    tested = {name for name, _, _ in FILE_EXAMPLES + DOCUMENT_EXAMPLES}
    assert {path.name for path in EXAMPLES.glob("relay-*")} == tested


def test_every_example_the_document_names_exists() -> None:
    named = re.findall(
        r"externalValue: \"\./schemas/examples/([^\"]+)\"", contracts.relay_path().read_text("utf-8")
    )
    assert named, "the document names its examples"
    assert all((EXAMPLES / name).is_file() for name in named), named


# ───────────────────────── declared, shipped, and read by the guards ─────────────────────────


def test_the_relay_schemas_are_declared_and_shipped() -> None:
    declared = {name for name in contracts.SCHEMA_FILES if name.startswith("relay-")}
    assert declared == set(FIVE) == {schema for _, schema, _ in FILE_EXAMPLES}
    assert contracts.relay_path().name == "relay.yaml"
    assert contracts.relay_path().parent == contracts.openapi_path().parent
    pyproject = (contracts.contracts_dir() / "pyproject.toml").read_text(encoding="utf-8")
    assert '"relay.yaml" = "tranibor_contracts/relay.yaml"' in pyproject, (
        "the relay's document must be in the wheel: the agent and the relay read the contract they were "
        "built against"
    )


def test_every_schema_reference_of_the_document_resolves() -> None:
    text = contracts.relay_path().read_text(encoding="utf-8")
    files = set(re.findall(r"\./schemas/([a-z-]+\.schema\.json)", text))
    assert files == set(FIVE), sorted(files)
    for ref in set(re.findall(r"#/components/(?:schemas|responses|parameters)/([A-Za-z]+)", text)):
        assert any(ref in DOCUMENT["components"][kind] for kind in ("schemas", "responses", "parameters")), (
            ref
        )


# ───────────────────────── schema and model declare the same fields ─────────────────────────


def _schema(name: str) -> dict[str, Any]:
    return contracts.load_schema(name)


SAME_FIELDS: list[tuple[str, dict[str, Any], type[BaseModel]]] = [
    ("hello", _schema("relay-hello.schema.json"), relay.RelayHello),
    ("open-stream", _schema("relay-open-stream.schema.json"), relay.RelayOpenStream),
    ("pairing-request", _schema("relay-pairing-request.schema.json"), relay.RelayPairingRequest),
    ("pairing-answer", _schema("relay-pairing-answer.schema.json"), relay.RelayPairingAnswer),
    (
        "pairing-answer.window",
        _schema("relay-pairing-answer.schema.json")["$defs"]["window"],
        relay.RelayProtocolWindow,
    ),
    ("metadata-line", _schema("relay-metadata-line.schema.json"), relay.RelayMetadataLine),
    ("document.ProtocolWindow", COMPONENTS["ProtocolWindow"], relay.RelayProtocolWindow),
    ("document.PairingPoll", COMPONENTS["PairingPoll"], relay.RelayPairingPoll),
    ("document.Challenge", COMPONENTS["Challenge"], relay.RelayChallenge),
    ("document.Welcome", COMPONENTS["Welcome"], relay.RelayWelcome),
    ("document.Close", COMPONENTS["Close"], relay.RelayClose),
    ("document.RelayProblem", COMPONENTS["RelayProblem"], relay.RelayProblem),
]


@pytest.mark.parametrize(("trail", "node", "model"), SAME_FIELDS, ids=[s[0] for s in SAME_FIELDS])
def test_a_schema_and_its_model_declare_the_same_fields(
    trail: str, node: dict[str, Any], model: type[BaseModel]
) -> None:
    gaps: list[str] = []
    _compare(trail, node, model, gaps)
    assert not gaps, "\n".join(gaps)
    assert node.get("properties"), f"{trail} declares no field: the comparison would prove nothing"


def test_every_relay_model_is_compared_with_a_schema() -> None:
    models = {
        value
        for value in vars(relay).values()
        if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == relay.__name__
    }
    assert models - {model for _, _, model in SAME_FIELDS} == {relay.Lenient}


def test_the_five_schemas_are_closed_and_what_else_the_relay_sends_is_open() -> None:
    """ADR 0230 §2: the five schemas are closed to extra fields, and their models are strict. What the
    relay sends besides them is read leniently: an agent of N-1 meets the challenge before the relay
    knows its protocol."""
    for name in FIVE:
        assert _schema(name)["additionalProperties"] is False, name
    closed = [relay.RelayHello, relay.RelayOpenStream, relay.RelayPairingRequest, relay.RelayPairingAnswer]
    for model in [*closed, relay.RelayMetadataLine, relay.RelayPairingPoll, relay.RelayProtocolWindow]:
        assert model.model_config.get("extra") == "forbid", model.__name__
    assert COMPONENTS["PairingPoll"]["additionalProperties"] is False
    assert COMPONENTS["ProtocolWindow"]["additionalProperties"] is False
    for name, model in (
        ("Challenge", relay.RelayChallenge),
        ("Welcome", relay.RelayWelcome),
        ("Close", relay.RelayClose),
        ("RelayProblem", relay.RelayProblem),
    ):
        assert "additionalProperties" not in COMPONENTS[name], name
        assert model.model_config.get("extra") == "ignore", name
    later = _json("relay-challenge.example.json") | {"a_later_field": 1}
    assert not list(_component("Challenge").iter_errors(later))
    assert "a_later_field" not in relay.RelayChallenge.model_validate(later).model_dump()


@pytest.mark.parametrize(
    ("schema", "model", "example"),
    [
        ("relay-hello.schema.json", relay.RelayHello, "relay-hello.example.json"),
        ("relay-open-stream.schema.json", relay.RelayOpenStream, "relay-open-stream.example.json"),
        (
            "relay-pairing-request.schema.json",
            relay.RelayPairingRequest,
            "relay-pairing-request.example.json",
        ),
        ("relay-pairing-answer.schema.json", relay.RelayPairingAnswer, "relay-pairing-answer.example.json"),
        ("relay-metadata-line.schema.json", relay.RelayMetadataLine, "relay-metadata-line.example.json"),
    ],
)
def test_a_field_outside_a_closed_schema_is_refused_by_both(
    schema: str, model: type[BaseModel], example: str
) -> None:
    assert _both(schema, model, _json(example) | {"later": "x"}) == (False, False)


# ───────────────────────── the order to dial back names no destination ─────────────────────────


def test_the_order_to_dial_back_has_no_host_and_no_port_and_is_closed() -> None:
    """ADR 0230 §3 and "Why this is not ADR 0147's refused relay": neither the relay nor the control
    plane can name a destination. The schema has no field that could, and refuses one that tries."""
    node = _schema("relay-open-stream.schema.json")
    names = set(node["properties"]) | set(relay.RelayOpenStream.model_fields)
    assert names == {"type", "stream", "ticket", "client_ip"}
    words = {word for name in names for word in name.split("_")}
    assert not words & {"host", "hostname", "port", "address", "target", "url", "uri", "destination"}
    assert node["additionalProperties"] is False
    order = _json("relay-open-stream.example.json")
    for field, value in (
        ("host", "10.0.0.5"),
        ("port", 22),
        ("target", "internal.example:443"),
        ("url", "https://internal.example"),
    ):
        assert _both("relay-open-stream.schema.json", relay.RelayOpenStream, order | {field: value}) == (
            False,
            False,
        ), field


def test_the_document_names_no_destination_anywhere() -> None:
    """No path parameter, no message and no component of the document names a host or a port to
    connect to; the one path parameter is a stream's id."""
    parameters = [
        parameter
        for item in DOCUMENT["paths"].values()
        for parameter in item.get("parameters", [])
        + [p for verb, op in item.items() if verb in VERBS for p in op.get("parameters", [])]
    ]
    assert parameters == [{"$ref": "#/components/parameters/StreamId"}]
    assert DOCUMENT["components"]["parameters"]["StreamId"]["name"] == "stream"
    for name, schema in COMPONENTS.items():
        fields = {word for field in schema.get("properties", {}) for word in field.split("_")}
        assert not fields & {"host", "port", "target", "destination"}, name
    assert "edge:8443" in DOCUMENT["x-target"]["rule"]


def test_a_client_address_is_an_ip_address_and_nothing_else() -> None:
    order = _json("relay-open-stream.example.json")
    for good in ("203.0.113.24", "2001:db8::1"):
        assert _both("relay-open-stream.schema.json", relay.RelayOpenStream, order | {"client_ip": good}) == (
            True,
            True,
        )
    for bad in ("phone.example", "203.0.113.24:443", "10.0.0.256", ""):
        assert _both("relay-open-stream.schema.json", relay.RelayOpenStream, order | {"client_ip": bad}) == (
            False,
            False,
        ), bad


# ───────────────────────── the pairing ─────────────────────────


def test_a_pairing_request_that_names_an_id_is_refused_as_an_extra_field() -> None:
    """ADR 0230 §6: the relay draws the id; nobody chooses one."""
    request = _json("relay-pairing-request.example.json")
    assert "id" not in _schema("relay-pairing-request.schema.json")["properties"]
    for field in ("id", "hostname", "relay_id"):
        both = _both(
            "relay-pairing-request.schema.json", relay.RelayPairingRequest, request | {field: "kq7mz3xw2pa5"}
        )
        assert both == (False, False), field


def _answer(status: str, **fields: Any) -> dict[str, Any]:
    base = {"status": status, "server_time": "2026-11-26T09:41:02Z", "protocol": {"current": 1, "minimum": 1}}
    return base | fields


PENDING = {
    "device_code": "Gm4cJx0aT9qVb2Rk7LsNw1EyZp5Hd8Ui3Fo6Ct0Xj2Q",
    "user_code": "BCDF-KMPT",
    "verification_uri": "https://relay.example/pair",
    "expires_at": "2026-11-26T09:51:02Z",
    "interval_s": 5,
}
APPROVED = {"id": "kq7mz3xw2pa5", "hostname": "kq7mz3xw2pa5.relay.example"}


@pytest.mark.parametrize(
    ("what", "document", "accepted"),
    [
        ("pending", _answer("pending", **PENDING), True),
        ("approved", _answer("approved", **APPROVED), True),
        ("denied", _answer("denied"), True),
        ("expired", _answer("expired"), True),
        ("pending without its user code", _answer("pending", **{**PENDING, "user_code": None}), False),
        ("pending with an id", _answer("pending", **PENDING, **APPROVED), False),
        ("approved without a hostname", _answer("approved", id="kq7mz3xw2pa5"), False),
        (
            "approved with a device code",
            _answer("approved", **APPROVED, device_code=PENDING["device_code"]),
            False,
        ),
        ("denied with an id", _answer("denied", **APPROVED), False),
        ("expired with a user code", _answer("expired", user_code="BCDF-KMPT"), False),
        (
            "a user code outside RFC 8628's consonants",
            _answer("pending", **{**PENDING, "user_code": "ABCD-EFGH"}),
            False,
        ),
        ("polling faster than the floor", _answer("pending", **{**PENDING, "interval_s": 1}), False),
        (
            "a verification page over http",
            _answer("pending", **{**PENDING, "verification_uri": "http://relay.example/pair"}),
            False,
        ),
    ],
)
def test_a_pairing_answer_carries_what_its_status_says_by_the_schema_and_the_model(
    what: str, document: dict[str, Any], accepted: bool
) -> None:
    assert _both("relay-pairing-answer.schema.json", relay.RelayPairingAnswer, document) == (
        accepted,
        accepted,
    ), what


def test_an_approved_hostname_is_its_id_under_the_relay_domain() -> None:
    other = _answer("approved", id="kq7mz3xw2pa5", hostname="aaaaaaaaaaaa.relay.example")
    assert not list(_file("relay-pairing-answer.schema.json").iter_errors(other))
    assert not _accepts(relay.RelayPairingAnswer, other), "the model holds what the schema cannot say"


# ───────────────────────── ids ─────────────────────────


def test_an_id_is_sixty_random_bits_in_twelve_base32_characters() -> None:
    """ADR 0230 §6: 60 random bits, written as 12 base32 characters; nobody chooses one."""
    assert relay.ID_BITS == 60 and relay.ID_LENGTH == 12 and relay.ID_BITS == 5 * relay.ID_LENGTH
    assert DOCUMENT["x-ids"]["bits"] == relay.ID_BITS and DOCUMENT["x-ids"]["length"] == relay.ID_LENGTH
    assert set(DOCUMENT["x-ids"]["alphabet"]) == set("abcdefghijklmnopqrstuvwxyz234567")
    assert relay.encode_relay_id(0) == "a" * 12
    assert relay.encode_relay_id((1 << 60) - 1) == "7" * 12
    for value in (0, 1, 31, 32, 0x0123456789ABCDE, (1 << 60) - 1):
        assert relay.decode_relay_id(relay.encode_relay_id(value)) == value
    for value in (-1, 1 << 60):
        with pytest.raises(ValueError, match="60 bits"):
            relay.encode_relay_id(value)
    for spelled in ("KQ7MZ3XW2PA5", "kq7mz3xw2pa", "kq7mz3xw2pa51", "kq7mz3xw2pa0"):
        with pytest.raises(ValueError, match="base32"):
            relay.decode_relay_id(spelled)
    drawn = {relay.new_relay_id() for _ in range(2000)}
    assert len(drawn) == 2000
    assert all(re.fullmatch(relay.RELAY_ID_PATTERN, one) for one in drawn)
    # Every character of the alphabet comes out, and the first carries all five of its bits.
    assert {one[0] for one in drawn} == set(DOCUMENT["x-ids"]["alphabet"])


def test_the_id_pattern_is_the_same_in_every_document() -> None:
    nodes = [
        _schema("relay-hello.schema.json")["properties"]["id"],
        _schema("relay-pairing-answer.schema.json")["properties"]["id"],
        _schema("relay-metadata-line.schema.json")["properties"]["install_id"],
        COMPONENTS["Welcome"]["properties"]["id"],
    ]
    assert {node["pattern"] for node in nodes} == {relay.RELAY_ID_PATTERN}
    hostnames = [
        _schema("relay-pairing-answer.schema.json")["properties"]["hostname"],
        COMPONENTS["Welcome"]["properties"]["hostname"],
        _schema("local-status.schema.json")["$defs"]["relay"]["properties"]["hostname"],
    ]
    assert {node["pattern"] for node in hostnames} == {relay.RELAY_HOSTNAME_PATTERN}
    assert {node["maxLength"] for node in hostnames} == {relay.MAX_HOSTNAME_LENGTH}
    stream = DOCUMENT["components"]["parameters"]["StreamId"]["schema"]["pattern"]
    assert stream == _schema("relay-open-stream.schema.json")["properties"]["stream"]["pattern"]
    assert stream == relay.STREAM_ID_PATTERN
    assert _schema("relay-open-stream.schema.json")["properties"]["ticket"]["pattern"] == relay.TICKET_PATTERN
    assert COMPONENTS["Challenge"]["properties"]["nonce"]["pattern"] == relay.NONCE_PATTERN
    assert COMPONENTS["PairingPoll"]["properties"]["device_code"]["pattern"] == relay.DEVICE_CODE_PATTERN
    key = _schema("relay-pairing-request.schema.json")["properties"]["signing_key"]["pattern"]
    assert key == relay.PUBLIC_KEY_PATTERN == outpost.PUBLIC_KEY_PATTERN


def test_a_ticket_and_a_device_code_hold_256_bits_at_least() -> None:
    """What opens something is not guessable: 43 base64url characters carry 258 bits."""
    for pattern in (relay.TICKET_PATTERN, relay.DEVICE_CODE_PATTERN):
        assert re.fullmatch(pattern, "A" * 43) and not re.fullmatch(pattern, "A" * 42)


# ───────────────────────── the metadata line ─────────────────────────


def test_a_metadata_line_holds_the_eight_fields_and_nothing_else() -> None:
    """ADR 0230 §7: no ALPN, no cipher list, no server name, no byte of the stream."""
    fields = {
        "install_id",
        "started_at",
        "ended_at",
        "client_ip",
        "bytes_in",
        "bytes_out",
        "duration_ms",
        "close_reason",
    }
    node = _schema("relay-metadata-line.schema.json")
    assert set(node["properties"]) == fields == set(relay.RelayMetadataLine.model_fields)
    assert set(node["required"]) == fields
    line = _json("relay-metadata-line.example.json")
    for extra in ("alpn", "cipher_suites", "server_name", "sni", "path", "headers", "body", "bytes"):
        assert _both("relay-metadata-line.schema.json", relay.RelayMetadataLine, line | {extra: "x"}) == (
            False,
            False,
        ), extra


def test_a_line_names_its_installation_exactly_when_the_connection_was_routed() -> None:
    line = _json("relay-metadata-line.example.json")
    schema = "relay-metadata-line.schema.json"
    for reason in relay.RelayCloseReason:
        named = line | {"close_reason": reason.value}
        unnamed = named | {"install_id": None}
        routed = reason not in relay.UNROUTED
        assert _both(schema, relay.RelayMetadataLine, named) == (routed, routed), reason
        assert _both(schema, relay.RelayMetadataLine, unnamed) == (not routed, not routed), reason
    assert _schema(schema)["if"]["properties"]["close_reason"]["enum"] == sorted(
        relay.UNROUTED, key=list(relay.RelayCloseReason).index
    )


def test_a_line_ends_after_it_starts_and_counts_nothing_negative() -> None:
    line = _json("relay-metadata-line.example.json")
    assert not _accepts(relay.RelayMetadataLine, line | {"ended_at": "2026-11-26T10:00:00Z"})
    for field in ("bytes_in", "bytes_out", "duration_ms"):
        assert _both("relay-metadata-line.schema.json", relay.RelayMetadataLine, line | {field: -1}) == (
            False,
            False,
        ), field


# ───────────────────────── one closed list for each set of codes ─────────────────────────


def test_the_closed_lists_are_the_documents_lists() -> None:
    assert COMPONENTS["Close"]["properties"]["code"]["enum"] == [code.value for code in relay.RelayCloseCode]
    assert COMPONENTS["RelayProblem"]["properties"]["code"]["enum"] == [
        code.value for code in relay.RelayProblemCode
    ]
    assert _schema("relay-pairing-answer.schema.json")["properties"]["status"]["enum"] == [
        status.value for status in relay.RelayPairingStatus
    ]
    assert _schema("relay-metadata-line.schema.json")["properties"]["close_reason"]["enum"] == [
        reason.value for reason in relay.RelayCloseReason
    ]
    for code in relay.RelayCloseCode:
        assert f"`{code.value}`" in COMPONENTS["Close"]["properties"]["code"]["description"], code
    for code in relay.RelayProblemCode:
        assert (
            f"`{code.value}` ({relay.PROBLEM_STATUS[code]})"
            in COMPONENTS["RelayProblem"]["properties"]["code"]["description"]
        ), code


def _responses(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for status, response in operation["responses"].items():
        reference = response.get("$ref")
        found[status] = (
            DOCUMENT["components"]["responses"][reference.rsplit("/", 1)[-1]] if reference else response
        )
    return found


def test_each_problem_code_travels_under_one_status() -> None:
    assert set(relay.PROBLEM_STATUS) == set(relay.RelayProblemCode)
    seen: dict[str, set[int]] = {}
    for item in DOCUMENT["paths"].values():
        for verb, operation in item.items():
            if verb not in VERBS:
                continue
            for status, response in _responses(operation).items():
                for code in response.get("x-codes", []):
                    seen.setdefault(code, set()).add(int(status))
    assert set(seen) == {code.value for code in relay.RelayProblemCode}
    for code, statuses in seen.items():
        assert statuses == {relay.PROBLEM_STATUS[relay.RelayProblemCode(code)]}, code


def test_an_unknown_code_is_read_and_named_unknown() -> None:
    close = relay.RelayClose.model_validate(_json("relay-close.example.json") | {"code": "a_later_code"})
    assert close.known_code is None
    assert relay.RelayClose.model_validate(_json("relay-close.example.json")).known_code is (
        relay.RelayCloseCode.PROTOCOL_UNSUPPORTED
    )
    problem = relay.RelayProblem.model_validate(_json("relay-problem.example.json"))
    assert problem.known_code is relay.RelayProblemCode.RATE_LIMITED


# ───────────────────────── the protocol, the paths and the bounds ─────────────────────────


def test_the_relay_serves_n_and_n_minus_1_and_refuses_older() -> None:
    """ADR 0230 §2: the server serves protocol N and N-1, and refuses an older hello naming the minimum."""
    assert DOCUMENT["info"]["version"] == f"v{relay.RELAY_PROTOCOL}"
    assert DOCUMENT["x-protocol"]["version"] == relay.RELAY_PROTOCOL
    assert DOCUMENT["x-bounds"]["protocols_served"] == relay.PROTOCOLS_SERVED == 2
    assert relay.protocol_window() == relay.RelayProtocolWindow(current=1, minimum=1)
    window = relay.protocol_window(5)
    assert (window.current, window.minimum) == (5, 4)
    assert [protocol for protocol in range(1, 8) if relay.is_served(protocol, window)] == [4, 5]
    assert "N and N-1" in DOCUMENT["x-protocol"]["served"]
    close = relay.RelayClose.model_validate(_json("relay-close.example.json"))
    assert close.protocol is not None and close.protocol.current - close.protocol.minimum == 1


def test_the_paths_are_the_pairing_the_control_and_the_data() -> None:
    operations = {
        (path, verb): operation["operationId"]
        for path, item in DOCUMENT["paths"].items()
        for verb, operation in item.items()
        if verb in VERBS
    }
    assert operations == {
        ("/relay/v1/pairing", "post"): "requestRelayPairing",
        ("/relay/v1/pairing/poll", "post"): "pollRelayPairing",
        ("/relay/v1/control", "get"): "openRelayControl",
        ("/relay/v1/data/{stream}", "get"): "openRelayStream",
    }
    assert all(path.startswith(relay.RELAY_PREFIX + "/") for path, _ in operations)
    control = DOCUMENT["x-control"]["messages"]
    assert control["hello"]["schema"]["$ref"] == "./schemas/relay-hello.schema.json"
    assert control["open_stream"]["schema"]["$ref"] == "./schemas/relay-open-stream.schema.json"
    assert {name: message["from"] for name, message in control.items()} == {
        "challenge": "relay",
        "hello": "agent",
        "welcome": "relay",
        "open_stream": "relay",
        "close": "relay",
    }
    assert DOCUMENT["x-metadata"]["line"]["$ref"] == "./schemas/relay-metadata-line.schema.json"


def test_the_bounds_of_the_document_are_the_constants() -> None:
    bounds = DOCUMENT["x-bounds"]
    assert bounds["iat_window_s"] == relay.IAT_WINDOW_S == 60
    assert bounds["hello_timeout_s"] == relay.HELLO_TIMEOUT_S
    assert bounds["ping_interval_s"] == relay.PING_INTERVAL_S
    assert bounds["max_missed_pongs"] == relay.MAX_MISSED_PONGS
    assert bounds["ticket_ttl_s"] == relay.TICKET_TTL_S
    assert bounds["pairing_ttl_s"] == relay.PAIRING_TTL_S
    assert bounds["pairing_interval_s"] == relay.PAIRING_INTERVAL_S
    assert bounds["client_hello_max_bytes"] == relay.CLIENT_HELLO_MAX_BYTES == 16_384
    assert bounds["client_hello_timeout_s"] == relay.CLIENT_HELLO_TIMEOUT_S == 5
    assert bounds["max_control_message_bytes"] == relay.MAX_CONTROL_MESSAGE_BYTES
    assert bounds["max_body_bytes"] == relay.MAX_BODY_BYTES
    caps = {
        operation["operationId"]: operation["x-max-body-bytes"]
        for item in DOCUMENT["paths"].values()
        for verb, operation in item.items()
        if verb == "post"
    }
    assert caps == {
        "requestRelayPairing": relay.MAX_BODY_BYTES["pairing"],
        "pollRelayPairing": relay.MAX_BODY_BYTES["pairing_poll"],
    }
    interval = _schema("relay-pairing-answer.schema.json")["properties"]["interval_s"]
    assert interval["minimum"] == relay.PAIRING_INTERVAL_S


# ───────────────────────── what a signature binds ─────────────────────────


def _part(segment: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    return decoded


def test_the_request_signature_is_described_as_the_helpers_build_it() -> None:
    described = DOCUMENT["x-request-signature"]
    assert described["header"] == relay.SIGNATURE_HEADER
    assert described["type"] == relay.REQUEST_SIGNATURE_TYPE
    assert described["algorithm"] == relay.SIGNATURE_ALGORITHM == "EdDSA"
    assert described["claim_names"] == list(relay.REQUEST_SIGNATURE_CLAIMS) == list(described["claims"])
    claims = relay.request_signature_claims(
        method="post", url="https://relay.example/relay/v1/pairing", body=b"{}", iat=1
    )
    assert list(claims) == list(relay.REQUEST_SIGNATURE_CLAIMS)
    assert claims["htm"] == "POST" and claims["body_sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert relay.request_signature_header() == {"alg": "EdDSA", "typ": relay.REQUEST_SIGNATURE_TYPE}
    scheme = DOCUMENT["components"]["securitySchemes"]["relaySignature"]
    assert scheme["name"] == relay.SIGNATURE_HEADER
    assert DOCUMENT["components"]["securitySchemes"]["relayTicket"]["name"] == relay.TICKET_HEADER


def test_the_hello_signature_is_described_as_the_helpers_build_it_and_the_example_carries_it() -> None:
    """ADR 0230 §3: a compact JWS by the installation's Ed25519 key, binding the nonce the relay issued
    and an `iat` within 60 seconds."""
    described = DOCUMENT["x-hello-signature"]
    assert described["type"] == relay.HELLO_SIGNATURE_TYPE
    assert described["claim_names"] == list(relay.HELLO_SIGNATURE_CLAIMS) == list(described["claims"])
    hello = _json("relay-hello.example.json")
    challenge = _json("relay-challenge.example.json")
    header, claims, _ = hello["signature"].split(".")
    assert _part(header) == relay.hello_signature_header(hello["id"])
    assert _part(claims) == relay.hello_signature_claims(
        url=DOCUMENT["x-examples"]["control_url"],
        relay_id=hello["id"],
        protocol=hello["protocol"],
        nonce=challenge["nonce"],
        iat=_part(claims)["iat"],
    )
    signed_at = _part(claims)["iat"]
    challenged_at = relay.RelayChallenge.model_validate(challenge).server_time.timestamp()
    assert abs(signed_at - challenged_at) <= relay.IAT_WINDOW_S
    assert DOCUMENT["x-examples"]["control_url"].endswith(relay.RELAY_PREFIX + "/control")


def test_the_examples_are_signed_by_the_key_the_label_derives() -> None:
    """The example's key is test material anyone can derive from the label the document writes."""
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    exceptions = pytest.importorskip("cryptography.exceptions")
    seed = hashlib.sha256(DOCUMENT["x-examples"]["label"].encode("utf-8")).digest()
    public = ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key()
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert (
        base64.urlsafe_b64encode(raw).decode().rstrip("=")
        == _json("relay-pairing-request.example.json")["signing_key"]
    )
    header, claims, signature = _json("relay-hello.example.json")["signature"].split(".")
    public.verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{claims}".encode("ascii"))
    tampered = _part(claims) | {"nonce": "another-nonce-of-22-chars"}
    forged = (
        base64.urlsafe_b64encode(json.dumps(tampered, separators=(",", ":")).encode()).decode().rstrip("=")
    )
    with pytest.raises(exceptions.InvalidSignature):
        public.verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{forged}".encode("ascii"))


def test_the_document_parses_as_yaml_with_no_duplicate_keys() -> None:
    """`yaml.safe_load` keeps the last of two equal keys silently; a duplicate would hide a field."""

    class _Unique(yaml.SafeLoader):
        pass

    def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
        keys = [loader.construct_object(key) for key, _ in node.value]
        assert len(keys) == len(set(keys)), f"duplicate keys at line {node.start_mark.line + 1}"
        return loader.construct_mapping(node)

    _Unique.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    assert yaml.load(contracts.relay_path().read_text("utf-8"), Loader=_Unique) == DOCUMENT
