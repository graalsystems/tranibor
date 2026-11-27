"""The owner signs in on the loopback door; the edge names the door by listener (ADR 0228 §4-§6, S87-12).

A personal installation has two doors: the loopback door (the computer, `http://127.0.0.1`) and the relay
door (a phone, through the relay of ADR 0230). The edge writes `X-Choregos-Door` from the listener a
request came through, over whatever the client sent; the API never reads the door from `Host`. In that
posture a request without a door is misdirected (421), the health routes excepted.

`POST /auth/owner` exchanges a ticket the owner key signed (`tranibor_core.owner_ticket`) for a session of
the installation's one person, on the loopback door only. Each door has its own cookie, accepted on that
door only. Outside the personal posture nothing of it exists, and nothing else changes.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import tranibor_contracts as contracts
from httpx import AsyncClient, Response
from jsonschema import Draft202012Validator
from tranibor_core.owner_ticket import AUDIENCE, generate_owner_key_pair, sign_claims, sign_owner_ticket

from .conftest import login

LOOPBACK = {"X-Choregos-Door": "loopback"}
RELAY = {"X-Choregos-Door": "relay"}
OWNER = "me@home.test"
OWNER_ROUTE = "/api/v1/auth/owner"
METHODS_ROUTE = "/api/v1/auth/methods"


@pytest.fixture
def owner_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The owner key `choregos local init` makes: the API reads only its public half, from a file."""
    private, public = generate_owner_key_pair()
    path = tmp_path / "owner.pub"
    path.write_bytes(public)
    monkeypatch.setenv("TRANIBOR_OWNER_PUBLIC_KEY_FILE", str(path))
    return private


@pytest.fixture
async def owner(app: Any, org: str) -> str:
    """The installation's one person: an administrator of its organisation, as the bootstrap writes it."""
    from sqlalchemy import select
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        org_id = (await session.execute(select(Organization.id).where(Organization.slug == org))).scalar_one()
        user = User(email=OWNER, display_name="me")
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org_id, role="org_admin"))
        return str(user.id)


@pytest.fixture
def personal(app: Any, owner_key: bytes, monkeypatch: pytest.MonkeyPatch) -> Iterator[bytes]:
    """The application, switched to the personal posture once built: the settings are read per request."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_ENV", "personal")
    # The development sign-in is refused in production (S87-09): the settings could not be built with it.
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    reset_settings_cache()
    yield owner_key
    monkeypatch.undo()
    reset_settings_cache()


def _ticket(private: bytes, **claims: Any) -> str:
    if not claims:
        return sign_owner_ticket(private)
    issued = int(datetime.now(UTC).timestamp())
    return sign_claims(
        private, {"aud": AUDIENCE, "iat": issued, "exp": issued + 30, "jti": "j" * 32} | claims
    )


async def _exchange(client: AsyncClient, ticket: str, door: dict[str, str] | None = None) -> Response:
    return await client.post(OWNER_ROUTE, json={"ticket": ticket}, headers=LOOPBACK if door is None else door)


def _set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


# ───────────────────────── the owner key opens a session, on the loopback door ─────────────────────────


async def test_a_valid_ticket_opens_a_session_of_the_one_person(
    client: AsyncClient, owner: str, personal: bytes
) -> None:
    signed_in = await _exchange(client, _ticket(personal))
    assert signed_in.status_code == 204, signed_in.text
    me = await client.get("/api/v1/me", headers=LOOPBACK)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == OWNER
    # A recorded session (ADR 0130), listed under My account and opened by the owner key.
    sessions = (await client.get("/api/v1/me/sessions", headers=LOOPBACK)).json()["items"]
    assert [row["current"] for row in sessions] == [True]

    from sqlalchemy import select
    from tranibor_api.db.models import AuditLog, UserSession
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        (row,) = (await session.execute(select(UserSession))).scalars().all()
        assert (row.user_id, row.channel) == (owner, "owner")
        logins = (await session.execute(select(AuditLog).where(AuditLog.action == "auth.login"))).scalars()
        assert [entry.payload.get("canal") for entry in logins] == ["owner"]


async def test_the_session_is_freshly_authenticated(client: AsyncClient, owner: str, personal: bytes) -> None:
    """Its authentication time is the exchange: fresh for ADR 0170 and for ADR 0228 §3."""
    from tranibor_api.config import get_settings
    from tranibor_api.security import read_session

    before = int(datetime.now(UTC).timestamp())
    assert (await _exchange(client, _ticket(personal))).status_code == 204
    payload = read_session(client.cookies["choregos_session"], get_settings())
    assert payload is not None
    assert payload["auth_time"] >= before
    assert payload["door"] == "loopback"


async def test_a_ticket_is_taken_once(client: AsyncClient, owner: str, personal: bytes) -> None:
    ticket = _ticket(personal)
    assert (await _exchange(client, ticket)).status_code == 204
    client.cookies.clear()
    replayed = await _exchange(client, ticket)
    assert replayed.status_code == 401, replayed.text
    assert not _set_cookies(replayed)

    from sqlalchemy import func, select
    from tranibor_api.db.models import OwnerTicket
    from tranibor_api.db.session import session_scope

    # Recorded in the database, under a unique `jti`: it holds across replicas.
    async with session_scope() as session:
        assert (await session.execute(select(func.count()).select_from(OwnerTicket))).scalar_one() == 1


async def test_two_exchanges_of_one_ticket_at_once_open_one_session(
    app: Any, owner: str, personal: bytes
) -> None:
    """Two replicas given one ticket at one moment: the unique `jti` lets one win, the other is refused."""
    import asyncio

    from httpx import ASGITransport

    ticket = _ticket(personal)

    async def exchange() -> int:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as browser:
            return (await _exchange(browser, ticket)).status_code

    assert sorted(await asyncio.gather(exchange(), exchange())) == [204, 401]


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(lambda key: _ticket(key, iat=1_700_000_000, exp=1_700_000_030), id="expired"),
        pytest.param(
            lambda key: _ticket(key, exp=int(datetime.now(UTC).timestamp()) + 120), id="valid-over-60-seconds"
        ),
        pytest.param(lambda key: _ticket(key, aud="choregos-mcp"), id="another-audience"),
        pytest.param(lambda key: sign_owner_ticket(generate_owner_key_pair()[0]), id="another-key"),
        pytest.param(lambda key: sign_owner_ticket(key).rsplit(".", 1)[0] + ".", id="unsigned"),
        pytest.param(lambda key: "not.a.ticket", id="garbage"),
    ],
)
async def test_a_ticket_that_does_not_hold_is_refused_without_saying_why(
    client: AsyncClient, owner: str, personal: bytes, forge: Any
) -> None:
    refused = await _exchange(client, forge(personal))
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"] == "this sign-in link is not valid: run `choregos local open` again"
    assert not _set_cookies(refused)
    assert (await client.get("/api/v1/me", headers=LOOPBACK)).status_code == 401


async def test_the_relay_door_has_no_owner_sign_in(client: AsyncClient, owner: str, personal: bytes) -> None:
    """A client through the relay picks its `Host`: the door is the edge's, and has no owner route."""
    ticket = _ticket(personal)
    for headers in (RELAY, {**RELAY, "Host": "127.0.0.1"}):
        refused = await _exchange(client, ticket, headers)
        assert refused.status_code == 404, refused.text
        assert not _set_cookies(refused)
    # Nothing was taken: the same ticket still opens a session on the loopback door.
    assert (await _exchange(client, ticket)).status_code == 204


async def test_a_second_person_leaves_no_one_to_sign_in(
    client: AsyncClient, owner: str, personal: bytes
) -> None:
    """The owner is the one person who holds a membership; a machine account is not a person (ADR 0131)."""
    from sqlalchemy import select
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        org_id = (await session.execute(select(Organization.id))).scalar_one()
        robot = User(email="robot@home.test", display_name="robot", is_service=True)
        session.add(robot)
        await session.flush()
        session.add(Membership(user_id=robot.id, org_id=org_id, role="contributor"))
    assert (await _exchange(client, _ticket(personal))).status_code == 204

    client.cookies.clear()
    async with session_scope() as session:
        other = User(email="other@home.test", display_name="other")
        session.add(other)
        await session.flush()
        session.add(Membership(user_id=other.id, org_id=org_id, role="viewer"))
    refused = await _exchange(client, _ticket(personal))
    assert refused.status_code == 401, refused.text


# ───────────────────────── a cookie per door ─────────────────────────


async def test_the_loopback_cookie_is_choregos_session_without_secure(
    client: AsyncClient, owner: str, personal: bytes
) -> None:
    (cookie,) = _set_cookies(await _exchange(client, _ticket(personal)))
    name, _, attributes = cookie.partition("=")
    assert name == "choregos_session"
    flags = {part.strip().split("=")[0].lower() for part in attributes.split(";")[1:]}
    assert "secure" not in flags
    assert {"httponly", "path", "samesite", "max-age"} <= flags


async def test_the_relay_cookie_is_host_prefixed_and_secure(app: Any, personal: bytes) -> None:
    """The relay door's cookie: `__Host-choregos_session`, `Secure`, `Path=/`, no `Domain` (RFC 6265bis)."""
    from fastapi import Response as FastApiResponse
    from starlette.requests import Request
    from tranibor_api.config import get_settings
    from tranibor_api.doors import clear_session_cookie, set_session_cookie

    def request(door: str) -> Request:
        return Request({"type": "http", "headers": [(b"x-choregos-door", door.encode())], "path": "/"})

    response = FastApiResponse()
    set_session_cookie(response, request("relay"), get_settings(), "signed", max_age=60)
    (cookie,) = response.headers.getlist("set-cookie")
    assert cookie.startswith("__Host-choregos_session=signed;")
    assert "; Secure" in cookie and "; Path=/" in cookie and "Domain" not in cookie

    # Signing out on the relay door clears that cookie, with the attributes a browser requires of it.
    cleared = FastApiResponse()
    clear_session_cookie(cleared, request("relay"), get_settings())
    (gone,) = cleared.headers.getlist("set-cookie")
    assert gone.startswith("__Host-choregos_session=") and "; Secure" in gone and "Max-Age=0" in gone


async def test_a_cookie_is_accepted_only_on_the_door_that_issued_it(
    client: AsyncClient, owner: str, personal: bytes
) -> None:
    assert (await _exchange(client, _ticket(personal))).status_code == 204
    value = client.cookies["choregos_session"]
    client.cookies.clear()
    # The loopback cookie through the relay: under its own name it is not read there, and copied under the
    # relay door's name it names the other door.
    for cookie in (f"choregos_session={value}", f"__Host-choregos_session={value}"):
        refused = await client.get("/api/v1/me", headers={**RELAY, "Cookie": cookie})
        assert refused.status_code == 401, refused.text
    accepted = await client.get("/api/v1/me", headers={**LOOPBACK, "Cookie": f"choregos_session={value}"})
    assert accepted.status_code == 200, accepted.text


async def test_a_change_through_the_relay_with_its_cookie_comes_from_its_origin(
    client: AsyncClient, owner: str, personal: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The origin guard sees the relay door's cookie too, and the loopback origin is the console's."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_PUBLIC_URL", "https://me.relay.example")
    monkeypatch.setenv("TRANIBOR_LOOPBACK_URL", "http://127.0.0.1:7878")
    reset_settings_cache()
    foreign = await client.post(
        "/api/v1/auth/logout",
        headers={**RELAY, "Cookie": "__Host-choregos_session=x", "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403, foreign.text
    assert (await _exchange(client, _ticket(personal))).status_code == 204
    value = client.cookies["choregos_session"]
    client.cookies.clear()
    from_loopback = await client.post(
        "/api/v1/me/sessions/end-others",
        headers={**LOOPBACK, "Cookie": f"choregos_session={value}", "Origin": "http://127.0.0.1:7878"},
    )
    assert from_loopback.status_code == 200, from_loopback.text


# ───────────────────────── the door is the edge's ─────────────────────────


async def test_without_a_door_a_request_is_misdirected(
    client: AsyncClient, owner: str, personal: bytes
) -> None:
    for method, path in (("GET", "/api/v1/me"), ("GET", METHODS_ROUTE), ("POST", OWNER_ROUTE)):
        for headers in ({}, {"X-Choregos-Door": "lan"}, {"X-Choregos-Door": ""}):
            answer = await client.request(method, path, json={"ticket": "x"}, headers=headers)
            assert answer.status_code == 421, (path, headers, answer.text)
            assert answer.json()["title"] == "Misdirected Request"
    # Two doors on one request are none: the edge writes exactly one.
    twice = await client.get(
        METHODS_ROUTE, headers=[("X-Choregos-Door", "loopback"), ("X-Choregos-Door", "relay")]
    )
    assert twice.status_code == 421
    # The probes are not behind the edge.
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200


async def test_the_relay_door_does_not_reach_the_runs_api_or_the_outposts_door(
    client: AsyncClient, personal: bytes
) -> None:
    from tranibor_api.doors import reaches

    path = "/api/v1/internal/runs/r1/network"
    assert (await client.get(path, headers=LOOPBACK)).status_code == 401  # the run's API asks for its token
    hidden = await client.get(path, headers=RELAY)
    assert (hidden.status_code, hidden.json()) == (404, {"detail": "Not Found"})
    for path in ("/outposts/v1/poll", "/outposts", "/api/v1/internal", "/api/v1/internal/x"):
        assert not reaches("relay", path) and reaches("loopback", path)
    assert reaches("relay", "/api/v1/me") and reaches("relay", "/api/v1/internalise")


async def test_the_methods_follow_the_door(client: AsyncClient, personal: bytes) -> None:
    schema = {
        "$ref": "#/components/schemas/AuthMethods",
        "components": contracts.load_openapi()["components"],
    }
    loopback = (await client.get(METHODS_ROUTE, headers=LOOPBACK)).json()
    relay = (await client.get(METHODS_ROUTE, headers=RELAY)).json()
    for body in (loopback, relay):
        Draft202012Validator(schema).validate(body)
    assert loopback == {"posture": "personal", "door": "loopback", "methods": ["owner"]}
    # Pairing a phone (S87-13) and passkeys (S87-14) are named once they are served.
    assert relay == {"posture": "personal", "door": "relay", "methods": []}


async def _saml_provider_of(org: str) -> None:
    """A SAML provider configured for `org`, as `PUT /orgs/{org}/saml` writes it."""
    from sqlalchemy import select
    from tranibor_api.db.models import Organization, OrgSamlIdp
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        org_id = (await session.execute(select(Organization.id).where(Organization.slug == org))).scalar_one()
        session.add(
            OrgSamlIdp(
                org_id=org_id,
                idp_entity_id="https://idp.home.test",
                sso_url="https://idp.home.test/sso",
                certificate_pem="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                domains=["home.test"],
            )
        )


async def test_no_identity_provider_in_the_personal_posture(
    client: AsyncClient, org: str, personal: bytes
) -> None:
    """The OIDC routes answer 404, the development sign-in with them (refused in production, S87-09), and
    so do SAML's, even for an organisation that holds a provider: a provider's metadata, its sign-in and
    its assertion consumer service are an identity provider's way in."""
    for params in ({}, {"as": "admin@northwind.dev"}):
        assert (await client.get("/api/v1/auth/login", params=params, headers=LOOPBACK)).status_code == 404
    assert (await client.get("/api/v1/auth/callback?state=x", headers=LOOPBACK)).status_code == 404
    await _saml_provider_of(org)
    saml = f"/api/v1/saml/{org}"
    for door in (LOOPBACK, RELAY):
        metadata = await client.get(f"{saml}/metadata", headers=door)
        started = await client.get(f"{saml}/login", params={"redirect_to": "/"}, headers=door)
        consumed = await client.post(f"{saml}/acs", data={"SAMLResponse": "PHNhbWw+"}, headers=door)
        answers = (metadata, started, consumed)
        assert [answer.status_code for answer in answers] == [404] * 3, [answer.text for answer in answers]
        assert [answer.json() for answer in answers] == [{"detail": "Not Found"}] * 3


async def test_an_unreadable_owner_key_stops_the_start_up(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import get_settings, reset_settings_cache
    from tranibor_api.owner_sign_in import check_the_owner_key

    monkeypatch.setenv("TRANIBOR_ENV", "personal")
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    for content in (None, b"not a key"):
        path = tmp_path / "owner.pub"
        if content is not None:
            path.write_bytes(content)
        monkeypatch.setenv("TRANIBOR_OWNER_PUBLIC_KEY_FILE", str(path))
        reset_settings_cache()
        with pytest.raises(RuntimeError, match="TRANIBOR_OWNER_PUBLIC_KEY_FILE"):
            check_the_owner_key(get_settings())
    path.write_bytes(generate_owner_key_pair()[1])
    check_the_owner_key(get_settings())
    reset_settings_cache()


# ───────────────────────── outside the personal posture, nothing changes ─────────────────────────


async def test_outside_the_personal_posture_there_is_no_door_and_no_owner(
    client: AsyncClient, owner: str, owner_key: bytes
) -> None:
    ticket = _ticket(owner_key)
    for headers in ({}, LOOPBACK, RELAY):
        assert (await _exchange(client, ticket, headers)).status_code == 404
        methods = await client.get(METHODS_ROUTE, headers=headers)
        assert methods.json() == {"posture": "organisation", "door": None, "methods": ["identity_provider"]}
    # The organisation's sign-in and its cookie are as they were, with or without a door header.
    await login(client, "admin@northwind.dev")
    assert (await client.get("/api/v1/me")).status_code == 200
    assert (await client.get("/api/v1/me", headers=RELAY)).status_code == 200


async def test_staging_and_prod_have_no_door_either(
    client: AsyncClient, owner: str, owner_key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    for environment in ("staging", "prod"):
        monkeypatch.setenv("TRANIBOR_ENV", environment)
        reset_settings_cache()
        assert (await client.get(METHODS_ROUTE)).json()["posture"] == "organisation"
        assert (await _exchange(client, _ticket(owner_key), {})).status_code == 404
        assert (await client.get("/api/v1/me")).status_code == 401  # not 421
    monkeypatch.undo()
    reset_settings_cache()


def test_a_ticket_lives_sixty_seconds_at_most() -> None:
    """What `POST /auth/owner` checks is what the signer refuses to make: no ticket outlives a minute."""
    private, _ = generate_owner_key_pair()
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        sign_owner_ticket(private, now, lifetime_s=int(timedelta(minutes=2).total_seconds()))
