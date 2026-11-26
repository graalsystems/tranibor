# SPDX-License-Identifier: Apache-2.0
"""S87-08, the REST half: personal runners, a personal installation's sign-in, subscription runs and the
turns a runner serves (ADRs 0226, 0227 and 0228).

Contract only: no route serves any of this yet. Each operation is in `CONTRACT_ONLY` of
`apps/api/tests/test_contract_conformance.py` with the story that serves it, and each new `/me` and `/auth`
path in `NO_CLIENT_YET` of `tests/web/test_api_routes_have_a_client.py` with the story that gives it a client.
Every addition is optional or new (ADR 0168): what a client sent or read before still validates.
"""

from __future__ import annotations

from typing import Any

import tranibor_contracts as contracts
from jsonschema import Draft202012Validator

OPENAPI = contracts.load_openapi()
PATHS: dict[str, Any] = OPENAPI["paths"]
SCHEMAS: dict[str, Any] = OPENAPI["components"]["schemas"]
VERBS = {"get", "post", "put", "patch", "delete"}
FINGERPRINT = "SHA256:" + "A" * 43


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator({"components": OPENAPI["components"], **SCHEMAS[name]})


def _valid(name: str, document: Any) -> bool:
    return not list(_validator(name).iter_errors(document))


def _operations(prefix: str) -> dict[str, tuple[str, str]]:
    return {
        operation["operationId"]: (path, verb)
        for path, item in PATHS.items()
        if path.startswith(prefix)
        for verb, operation in item.items()
        if verb in VERBS
    }


def _operation(operation_id: str) -> dict[str, Any]:
    for item in PATHS.values():
        for verb, operation in item.items():
            if verb in VERBS and operation["operationId"] == operation_id:
                found: dict[str, Any] = operation
                return found
    raise AssertionError(f"{operation_id} is not in the contract")


def _schema_of(operation_id: str, code: str) -> dict[str, Any]:
    reference = _operation(operation_id)["responses"][code]["content"]["application/json"]["schema"]["$ref"]
    found: dict[str, Any] = SCHEMAS[reference.rsplit("/", 1)[-1]]
    return found


def _instance(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "laptop-1",
        "status": "active",
        "fingerprint": FINGERPRINT,
        "daemon_version": "0.30.0",
        "protocol": 2,
        "modes": ["docker"],
        "providers": ["anthropic"],
        "capacity": 1,
        "runs_running": 0,
        "facts": {"hostname": "laptop"},
        "enrolled_at": "2026-11-26T10:00:00Z",
        "approval_step_up_minutes": 10,
        "approved_at": "2026-11-26T10:01:00Z",
        "last_seen_at": "2026-11-26T10:02:00Z",
        "online": True,
    }
    return base | over


def test_a_personal_runner_is_managed_by_its_owner_under_me_and_never_under_outposts() -> None:
    """ADR 0226 §2: the six routes of `/me/runners`; an administrator's routes live under `/outposts`."""
    assert _operations("/me/runners") == {
        "listMyRunners": ("/me/runners", "get"),
        "createMyRunnerEnrolment": ("/me/runners/{org}/enrolments", "post"),
        "approveMyRunnerInstance": ("/me/runners/instances/{instance}/approval", "post"),
        "pauseMyRunner": ("/me/runners/{org}/pause", "post"),
        "resumeMyRunner": ("/me/runners/{org}/resume", "post"),
        "revokeMyRunner": ("/me/runners/{org}/revocation", "post"),
    }
    assert not [path for path in PATHS if path.startswith("/me/runners") and "/outposts" in path]
    # The list is an object the server bounds (ADR 0206), never a bare array.
    assert _schema_of("listMyRunners", "200")["required"] == ["items"]
    # The owner approves against the fingerprint, in a person's session, as an administrator does.
    approve = _operation("approveMyRunnerInstance")
    body = approve["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/OutpostInstanceApprove"}
    assert {"401", "403", "404", "409", "422"} <= set(approve["responses"])
    assert "approval_requires_session" in approve["responses"]["403"]["description"]
    enrolment = _operation("createMyRunnerEnrolment")["responses"]["201"]["content"]["application/json"]
    assert enrolment["schema"] == {"$ref": "#/components/schemas/OutpostEnrolmentCreated"}
    # Revoking answers a machine that may be stolen: nothing stands in its way (ADR 0147 §5).
    assert "409" not in _operation("revokeMyRunner")["responses"]
    # A pause an administrator made is lifted by an administrator only.
    assert "403" in _operation("resumeMyRunner")["responses"]


def test_a_personal_runner_shows_its_machines_modes_and_provider_names_only() -> None:
    runner = {
        "org": "me",
        "slug": "runner-ada",
        "owner": "ada@example.org",
        "status": "active",
        "paused_by": None,
        "online": True,
        "last_seen_at": "2026-11-26T10:02:00Z",
        "instances": [_instance(), _instance(id="desk-2", status="pending", approved_at=None, modes=[])],
        "runs_waiting": 2,
        "created_at": "2026-11-26T09:00:00Z",
    }
    assert _valid("PersonalRunnerList", {"items": [runner]})
    assert _valid("PersonalRunner", runner | {"status": "paused", "paused_by": "administrator"})
    # Names only: nothing of a credential has a field, and a closed object takes none.
    assert not _valid("PersonalRunnerInstance", _instance(token="a-token-value"))
    assert not _valid("PersonalRunnerInstance", _instance(providers=[{"name": "anthropic", "token": "x"}]))
    assert not _valid("PersonalRunnerInstance", _instance(modes=["gvisor"]))
    assert not _valid("PersonalRunnerInstance", _instance(fingerprint="SHA256:short"))
    assert not _valid("PersonalRunner", runner | {"paused_by": "someone"})
    assert not _valid("PersonalRunner", runner | {"runs_waiting": -1})


def test_a_personal_installation_signs_in_without_a_password_by_door() -> None:
    """ADR 0228 §4: an owner ticket, a pairing made on the computer, passkeys; and the door says which."""
    public = {
        "getAuthMethods": "/auth/methods",
        "signInAsOwner": "/auth/owner",
        "exchangeDevicePairing": "/auth/device/session",
        "startPasskeySignIn": "/auth/passkey/options",
        "signInWithPasskey": "/auth/passkey",
    }
    for operation_id, path in public.items():
        assert _operations(path)[operation_id][0] == path
        assert _operation(operation_id)["security"] == [], operation_id
    # What a person does to their own sign-in takes their console session, a cookie: never a token.
    for operation_id in (
        "createMyDevicePairing",
        "listMyPasskeys",
        "registerMyPasskey",
        "startMyPasskeyRegistration",
        "deleteMyPasskey",
    ):
        assert _operation(operation_id)["security"] == [{"session": []}], operation_id
    pairing = _operation("createMyDevicePairing")
    assert "step_up_required" in pairing["responses"]["401"]["description"]
    assert _valid("AuthMethods", {"posture": "personal", "door": "loopback", "methods": ["owner"]})
    assert _valid("AuthMethods", {"posture": "organisation", "door": None, "methods": ["identity_provider"]})
    assert not _valid("AuthMethods", {"posture": "personal", "methods": ["password"]})
    assert not _valid("AuthMethods", {"posture": "team", "methods": []})
    assert not _valid("OwnerTicketExchange", {"ticket": "x" * 4097})
    assert not _valid("DevicePairingExchange", {"code": "abc", "user_code": "ABCD-EFGH"})
    link = {"url": "https://k3x.relay.example/pair#c0de", "expires_at": "2026-11-26T10:05:00Z"}
    assert _valid("DevicePairing", link)
    assert not _valid("DevicePairing", link | {"user_code": "ABCD-EFGH"})
    # A passkey is shown by what tells it apart, never by its key.
    passkey = {"id": "pk1", "name": "my phone", "created_at": "2026-11-26T10:00:00Z", "last_used_at": None}
    assert _valid("PasskeyList", {"items": [passkey]})
    assert not _valid("Passkey", passkey | {"public_key": "pQECAyYgASFYIA"})


def test_a_run_says_where_it_executed_and_on_which_credential() -> None:
    """ADR 0226 §7 and ADR 0227 §1: recorded facts, optional, so a run recorded before validates."""
    run = SCHEMAS["Run"]["allOf"][1]
    assert "required" not in run
    older = {"id": "r1", "status": "succeeded", "stage_role": "build"}
    assert _valid("Run", older)
    for place in ("platform", "outpost:0193b8a4-6d2e-7c1a-9f00-1234567890ab", None):
        assert _valid("Run", older | {"executed_at": place}), place
    for place in ("outpost:", "laptop", "outpost:../x", "Platform"):
        assert not _valid("Run", older | {"executed_at": place}), place
    for credential in ("gateway", "subscription", None):
        assert _valid("Run", older | {"credential": credential}), credential
    assert not _valid("Run", older | {"credential": "api_key"})


def test_a_conversation_records_where_its_turns_run() -> None:
    summary = SCHEMAS["ConversationSummary"]
    placement = summary["properties"]["placement"]
    assert placement["enum"] == ["platform", "personal_runner"] and placement["default"] == "platform"
    assert "placement" not in summary["required"], "a conversation opened before it was recorded is platform"
    # A conversation carries what its summary does.
    assert SCHEMAS["Conversation"]["allOf"][0] == {"$ref": "#/components/schemas/ConversationSummary"}


def test_a_runner_turn_carries_its_number_history_and_limits_and_a_hosted_turn_is_unchanged() -> None:
    """ADR 0226 §11: the platform's history is the source of truth; the runner keeps nothing between turns."""
    hosted = {"turn_id": "t1", "input": "hello", "protocol": "responses"}
    assert _valid("AgentSessionTurn", hosted)
    assert SCHEMAS["AgentSessionTurn"]["required"] == ["turn_id", "input", "protocol"]
    turn = hosted | {
        "number": 3,
        "instructions": "Be brief.",
        "instructions_sha256": "0" * 64,
        "history": [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
        "answered_turns": 2,
        "limits": {"max_tool_calls": 20, "max_minutes": 10, "builtin_web_tools": False},
    }
    assert _valid("AgentSessionTurn", turn)
    assert not _valid("AgentSessionTurn", turn | {"number": 0})
    assert not _valid("AgentSessionTurn", turn | {"instructions_sha256": "not-a-digest"})
    assert not _valid("AgentSessionTurn", turn | {"history": [{"role": "user", "text": "x"}] * 201})
    assert not _valid("AgentSessionTurn", turn | {"history": [{"role": "system", "text": "x"}]})
    assert not _valid("AgentSessionTurn", turn | {"limits": {"max_tool_calls": 20, "max_minutes": 10}})


def test_a_turn_result_names_how_a_subscription_turn_ends_and_how_the_runner_joined() -> None:
    status = SCHEMAS["AgentSessionTurnResult"]["properties"]["status"]["enum"]
    before = ["answered", "failed", "not_ready", "timeout", "max_turns", "budget_exceeded"]
    assert status[: len(before)] == before, "a value a pod already sends keeps its meaning"
    assert set(status) - set(before) == {"cancelled", "subscription_limited", "subscription_unauthorized"}
    assert _valid("AgentSessionTurnResult", {"status": "answered", "text": "ok"})
    runner = {"status": "subscription_limited", "continuity": "recap", "adapter_version": "0.9.1"}
    assert _valid("AgentSessionTurnResult", runner)
    assert not _valid("AgentSessionTurnResult", runner | {"continuity": "resumed"})
    assert not _valid("AgentSessionTurnResult", runner | {"adapter_version": "v" * 65})


def test_a_runner_streams_a_turn_in_bounded_numbered_batches_under_its_lease() -> None:
    """ADR 0226 §11 and ADR 0227 (amending ADR 0134 §6): numbered per turn, refused once the lease is lost."""
    path = "/internal/agent-sessions/{id}/turns/{turn_id}/deltas"
    deltas = PATHS[path]["post"]
    assert deltas["operationId"] == "postAgentSessionTurnDeltas"
    assert deltas["security"] == [{"runToken": []}]
    assert {"409", "410"} <= set(deltas["responses"])
    assert "lease_lost" in deltas["responses"]["409"]["description"]
    assert "lease_lost" in _operation("postAgentSessionTurnResult")["responses"]["409"]["description"]
    assert _schema_of("postAgentSessionTurnDeltas", "200")["required"] == ["last_n"]
    chunk = {"n": 1, "text": "x" * 8192}
    assert _valid("AgentSessionTurnDeltas", {"chunks": [chunk] * 64})
    assert not _valid("AgentSessionTurnDeltas", {"chunks": [chunk] * 65})
    assert not _valid("AgentSessionTurnDeltas", {"chunks": [{"n": 1, "text": "x" * 8193}]})
    assert not _valid("AgentSessionTurnDeltas", {"chunks": [{"n": 0, "text": "x"}]})
    assert not _valid("AgentSessionTurnDeltas", {"chunks": []})
