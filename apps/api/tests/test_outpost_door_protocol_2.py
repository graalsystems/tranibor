"""The outposts' door speaks protocol 2, and an organisation's outpost takes no runs lane (S87-08, S87-16,
ADR 0226 §1, §3).

Protocol 2 adds the runs lane inside the poll, with no verb: `runs` announced with `runner`,
`runs_running` and `runs_ended` in a poll, and `runs` and `cancel_runs` in its answer
(`packages/contracts/outpost-door.yaml`). The door's window is now protocol 2 down to protocol 1: a
daemon of either protocol is served as before. The lane is served to an instance of a person's own
outpost only (`test_outpost_door_runs.py`): at an organisation's outpost, which announces `tools` and
nothing else, an enrolment or a poll that announces `runs` is refused with the contract's 422, so that an
instance waiting there for an offer is told now instead of waiting for nothing; a poll of protocol 2 that
does not announce it is served like any other, and answered as protocol 1 answered it.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_contracts import NONCE_HEADER, OutpostInstanceStatus, load_outpost_door, outpost

from .kits import outpost_daemon as daemon_kit
from .kits import outposts as kit
from .kits.outpost_daemon import ENROL, POLL, SLUG, Daemon, refused

#: What a personal outpost's instance says it takes (the contract's example, `runner`).
RUNNER: dict[str, Any] = {
    "version": "0.30.0",
    "modes": ["docker"],
    "subscriptions": ["anthropic"],
    "offer_kinds": ["stage_run", "conversation_turn"],
    "run_capacity": 1,
}


@pytest.fixture(autouse=True)
def door(monkeypatch: pytest.MonkeyPatch) -> kit.Door:
    """Outposts on, the door given its origin and its key, the deployment its key wrapper."""
    daemon_kit.key_wrapper(monkeypatch)
    return kit.switch_on(monkeypatch)


@pytest.fixture
async def daemon(client: AsyncClient, admin: str) -> Daemon:
    """An instance a person approved, at the outpost `factory-floor`."""
    return await daemon_kit.approved(client)


def _runs_not_served(problem: dict[str, Any], where: tuple[str, ...]) -> None:
    assert "runs lane" in problem["detail"] and "organisation's outpost" in problem["detail"], problem
    assert where in {tuple(error["loc"]) for error in problem["errors"]}, problem


def test_the_door_serves_protocol_2_down_to_protocol_1() -> None:
    from tranibor_api.outposts import auth

    assert auth.window() == {"current": 2, "minimum": 1}
    assert auth.window() == {
        "current": outpost.OUTPOST_PROTOCOL,
        "minimum": auth.PROTOCOL_MINIMUM,
    }
    assert load_outpost_door()["x-protocol"]["window"] == auth.window(), "the window its document states"


async def test_a_poll_of_protocol_2_that_announces_no_runs_is_served(
    client: AsyncClient, daemon: Daemon, door: kit.Door
) -> None:
    """A daemon of protocol 2 that announces `tools` is handed its calls as before; what it says of the
    runs lane (nothing it runs, a run it ended) is taken and changes nothing at an organisation's
    outpost."""
    placed = await daemon_kit.place()
    response = await daemon.poll(
        client,
        protocol=2,
        runs_running=["5d7a2c19-8e4b-4f31-b6a0-3c9e1f72d845"],
        runs_ended=[{"id": "e1b94f06-27c3-4d8a-9f15-6a0c3b7e2d91", "exit_code": 0}],
    )
    assert response.status_code == 200, response.text
    answered = response.json()
    daemon_kit.is_a("PollAnswer", answered)
    outpost.OutpostPollAnswer.model_validate(answered)
    assert answered["protocol"] == {"current": 2, "minimum": 1}
    assert [call["id"] for call in answered["calls"]] == [placed.id]
    assert daemon.verifies(answered["calls"][0], [door.signing_public])
    # The door hands no offer and cancels no run: an organisation's outpost takes no runs lane.
    assert "runs" not in answered and "cancel_runs" not in answered
    assert response.headers[NONCE_HEADER]
    row = await daemon_kit.instance_row(str(daemon.instance_id))
    assert (row.protocol, row.capabilities) == (2, ["tools"])


async def test_a_poll_of_protocol_1_is_still_served(client: AsyncClient, daemon: Daemon) -> None:
    """A daemon nobody upgraded keeps its calls: the window's minimum is 1."""
    placed = await daemon_kit.place()
    response = await daemon.poll(client, protocol=1)
    assert response.status_code == 200, response.text
    assert response.json()["protocol"] == {"current": 2, "minimum": 1}
    assert [call["id"] for call in response.json()["calls"]] == [placed.id]


@pytest.mark.parametrize("capabilities", [["runs"], ["tools", "runs"]], ids=["runs-alone", "tools-and-runs"])
async def test_a_poll_of_protocol_2_that_announces_runs_is_refused_at_an_organisation_s_outpost(
    client: AsyncClient, daemon: Daemon, capabilities: list[str]
) -> None:
    """A body the contract takes, which this door refuses as the verb's own refusal: a 422 that names
    the lane and where the body announces it. The poll takes no slot and is handed nothing: a call
    placed for the outpost stays where it was."""
    placed = await daemon_kit.place()
    body = daemon.poll_body(protocol=2, capabilities=capabilities, runner=RUNNER)
    assert not daemon_kit.errors_against(load_outpost_door()["components"]["schemas"]["PollRequest"], body), (
        "the contract takes this poll: the refusal is the door's"
    )
    response = await daemon.send(client, POLL, body)
    problem = refused(response, "invalid_request", 422)
    _runs_not_served(problem, ("capabilities", str(capabilities.index("runs"))))
    assert NONCE_HEADER not in response.headers
    assert (await daemon_kit.call_row(placed.id)).state == "pending", "it was handed nothing"
    row = await daemon_kit.instance_row(str(daemon.instance_id))
    assert row.poll_holder is None and row.protocol == 1, "the refused poll took no slot and wrote nothing"
    # The same instance, announcing `tools` only, is served at once.
    served = await daemon.poll(client, protocol=2)
    assert served.status_code == 200 and [call["id"] for call in served.json()["calls"]] == [placed.id]


async def test_a_poll_of_protocol_1_that_announces_runs_is_refused_by_the_schema(
    client: AsyncClient, daemon: Daemon
) -> None:
    """No message of protocol 1 carries `runs`: the contract's model refuses it, where it says it."""
    problem = refused(await daemon.poll(client, capabilities=["runs"], runner=RUNNER), "invalid_request", 422)
    assert ("capabilities",) in {tuple(error["loc"]) for error in problem["errors"]}
    assert "organisation" not in problem["detail"], "the schema refused it before the door had to"


async def test_an_enrolment_that_announces_runs_is_refused_and_keeps_the_token(
    client: AsyncClient, admin: str
) -> None:
    """Of protocol 2, the contract takes it and the door refuses it; of protocol 1, the schema does. A
    refused enrolment does not use the token up, and enrols no instance."""
    from tranibor_api.db.models import OutpostInstance

    await kit.create(client, SLUG)
    token = await kit.token_for(client, SLUG)
    daemon = Daemon()
    announced = daemon.enrolment(token, protocol=2, capabilities=["runs"])
    daemon_kit.is_the_schema("outpost-enrolment-request.schema.json", announced)
    problem = refused(await daemon.send(client, ENROL, announced, kid=None), "invalid_request", 422)
    _runs_not_served(problem, ("capabilities", "0"))
    older = daemon.enrolment(token, capabilities=["tools", "runs"])
    problem = refused(await daemon.send(client, ENROL, older, kid=None), "invalid_request", 422)
    assert ("capabilities",) in {tuple(error["loc"]) for error in problem["errors"]}
    assert await kit.rows_of(SLUG, OutpostInstance) == []
    # The token still enrols the same key, announcing `tools`, at protocol 2.
    response = await daemon.send(client, ENROL, daemon.enrolment(token, protocol=2), kid=None)
    assert response.status_code == 201, response.text
    assert response.json()["protocol"] == {"current": 2, "minimum": 1}
    (row,) = await kit.rows_of(SLUG, OutpostInstance)
    assert (row.status, row.capabilities) == (OutpostInstanceStatus.PENDING.value, ["tools"])
