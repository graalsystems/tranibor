"""A phone is paired from the computer with a one-time link shown as a QR code (ADR 0228 §4, S87-13).

The owner, on the loopback door (the computer), signed in less than 10 minutes ago, creates a pairing
(`POST /me/device-pairings`): a device authorization of the device flow's table, already approved, of kind
`browser`, valid 5 minutes, whose device code travels only in the fragment of
`https://<relay host>/pair#<code>`. The phone's `/pair` page posts the code to `POST /auth/device/session`, on
the relay door only, which exchanges it once (one conditional update) for a browser session with the relay
door's cookie: listed under My account, ended there (ADR 0130), audited `device.paired`, and no fresher than
the sign-in that created the pairing.
"""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import tranibor_contracts as contracts
from httpx import ASGITransport, AsyncClient, Response
from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from tranibor_core import utcnow
from tranibor_core.owner_ticket import generate_owner_key_pair, sign_owner_ticket

from .conftest import login

LOOPBACK = {"X-Choregos-Door": "loopback"}
RELAY = {"X-Choregos-Door": "relay"}
OWNER = "me@home.test"
RELAY_URL = "https://me.relay.example"
PAIRINGS = "/api/v1/me/device-pairings"
EXCHANGE = "/api/v1/auth/device/session"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
RELAY_COOKIE = "__Host-choregos_session"


@pytest.fixture
def owner_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    private, public = generate_owner_key_pair()
    path = tmp_path / "owner.pub"
    path.write_bytes(public)
    monkeypatch.setenv("TRANIBOR_OWNER_PUBLIC_KEY_FILE", str(path))
    return private


@pytest.fixture
async def owner(app: Any, org: str) -> str:
    """The installation's one person, as the bootstrap writes it."""
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
    """The personal posture, reachable through a relay: its public URL is the relay's https host."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_ENV", "personal")
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    monkeypatch.setenv("TRANIBOR_PUBLIC_URL", RELAY_URL)
    monkeypatch.setenv("TRANIBOR_LOOPBACK_URL", "http://127.0.0.1:7878")
    reset_settings_cache()
    yield owner_key
    monkeypatch.undo()
    reset_settings_cache()


@pytest.fixture
async def computer(app: Any, owner: str, personal: bytes) -> AsyncIterator[AsyncClient]:
    """The owner's browser on the computer, signed in with the owner key a moment ago."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:7878") as http:
        signed_in = await http.post(
            "/api/v1/auth/owner", json={"ticket": sign_owner_ticket(personal)}, headers=LOOPBACK
        )
        assert signed_in.status_code == 204, signed_in.text
        yield http


@pytest.fixture
async def phones(app: Any) -> AsyncIterator[Any]:
    """Phones reaching the installation through the relay: https, so that a `Secure` cookie is sent back."""
    opened: list[AsyncClient] = []

    def open_one() -> AsyncClient:
        http = AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.7", 50000)),
            base_url=RELAY_URL,
            headers={"User-Agent": PHONE},
        )
        opened.append(http)
        return http

    yield open_one
    for http in opened:
        await http.aclose()


async def _pair(computer: AsyncClient) -> dict[str, Any]:
    created = await computer.post(PAIRINGS, headers=LOOPBACK)
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


def _code(pairing: dict[str, Any]) -> str:
    return str(pairing["url"]).split("#", 1)[1]


async def _exchange(phone: AsyncClient, code: str, door: dict[str, str] | None = None) -> Response:
    return await phone.post(EXCHANGE, json={"code": code}, headers=RELAY if door is None else door)


async def _rows() -> list[Any]:
    from tranibor_api.db.models import DeviceAuthorization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return list((await session.execute(select(DeviceAuthorization))).scalars().all())


def _stale(computer: AsyncClient, minutes: int) -> None:
    """The same recorded session, as if its sign-in were `minutes` old."""
    from tranibor_api.config import get_settings
    from tranibor_api.security import read_session, sign_session

    settings = get_settings()
    payload = read_session(computer.cookies["choregos_session"], settings)
    assert payload is not None
    stale = {**payload, "auth_time": int(time.time()) - minutes * 60}
    (held,) = [cookie for cookie in computer.cookies.jar if cookie.name == "choregos_session"]
    computer.cookies.set(
        "choregos_session", sign_session(stale, settings), domain=held.domain, path=held.path
    )


def _schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": contracts.load_openapi()["components"]}
    )


# ───────────────────────── the computer creates the pairing ─────────────────────────


async def test_the_owner_on_the_computer_creates_a_five_minute_link_to_the_relay(
    computer: AsyncClient,
) -> None:
    created = await computer.post(PAIRINGS, headers=LOOPBACK)
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    pairing = created.json()
    _schema("DevicePairing").validate(pairing)
    assert pairing["url"].startswith(f"{RELAY_URL}/pair#")
    code = _code(pairing)
    assert len(code) >= 43 and "/" not in code and "?" not in code
    (row,) = await _rows()
    # A device authorization of the device flow's table, already approved, of its own kind, for 5 minutes.
    assert (row.client_kind, row.status, row.user_id is not None) == ("browser", "approved", True)
    expires = datetime.fromisoformat(pairing["expires_at"].replace("Z", "+00:00"))
    created_at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    assert abs((expires - created_at) - timedelta(minutes=5)) < timedelta(seconds=2)
    # The platform keeps the code's digest only.
    assert row.device_hash == hashlib.sha256(code.encode()).hexdigest()
    assert code not in {row.user_code_hash, row.client_name, row.org_slug}

    from tranibor_api.db.models import AuditLog
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "device.pairing.create" in actions


async def test_creation_is_refused_with_a_sign_in_older_than_ten_minutes(computer: AsyncClient) -> None:
    _stale(computer, 11)
    refused = await computer.post(PAIRINGS, headers=LOOPBACK)
    assert refused.status_code == 401, refused.text
    problem = refused.json()
    assert problem["errors"][0]["error"] == "step_up_required"
    assert "10 min" in problem["detail"]
    # The step-up names the loopback door's way to sign in again, never the identity provider's.
    assert "auth/login" not in problem["errors"][0]["reauth"]
    assert await _rows() == []
    _stale(computer, 9)
    assert (await computer.post(PAIRINGS, headers=LOOPBACK)).status_code == 201


async def test_creation_is_refused_on_the_relay_door(computer: AsyncClient) -> None:
    value = computer.cookies["choregos_session"]
    for cookie in (f"choregos_session={value}", f"{RELAY_COOKIE}={value}"):
        refused = await computer.post(PAIRINGS, headers={**RELAY, "Cookie": cookie})
        assert refused.status_code == 404, refused.text
    assert await _rows() == []


async def test_creation_needs_the_console_session_not_a_token(app: Any, computer: AsyncClient) -> None:
    token = await computer.post("/api/v1/me/tokens", json={"name": "script"}, headers=LOOPBACK)
    assert token.status_code == 201, token.text
    bearer = {**LOOPBACK, "Authorization": f"Bearer {token.json()['token']}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:7878") as script:
        assert (await script.get("/api/v1/me", headers=bearer)).status_code == 200  # the token itself holds
        refused = await script.post(PAIRINGS, headers=bearer)
    assert refused.status_code == 401, refused.text
    # Refused for what it is, whatever its age: a token never pairs a phone, however fresh.
    assert "with your own session" in refused.json()["detail"]
    assert "errors" not in refused.json()
    assert await _rows() == []


async def test_creation_without_a_relay_hostname_is_a_clear_409(
    computer: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache

    for public in ("http://localhost:3000", "http://127.0.0.1:7878", "https://"):
        monkeypatch.setenv("TRANIBOR_PUBLIC_URL", public)
        reset_settings_cache()
        refused = await computer.post(PAIRINGS, headers=LOOPBACK)
        assert refused.status_code == 409, (public, refused.text)
        assert "choregos local relay enable" in refused.json()["detail"]
    assert await _rows() == []


# ───────────────────────── the phone exchanges it, once, through the relay ─────────────────────────


async def test_the_phone_exchanges_the_code_for_a_relay_session_of_the_owner(
    app: Any, computer: AsyncClient, phones: Any
) -> None:
    phone = phones()
    paired = await _exchange(phone, _code(await _pair(computer)))
    assert paired.status_code == 204, paired.text
    (cookie,) = paired.headers.get_list("set-cookie")
    assert cookie.startswith(f"{RELAY_COOKIE}=") and "; Secure" in cookie and "Domain" not in cookie
    me = await phone.get("/api/v1/me", headers=RELAY)
    assert me.status_code == 200, me.text
    assert me.json()["email"] == OWNER
    # The relay door's cookie opens nothing on the loopback door.
    value = phone.cookies[RELAY_COOKIE]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:7878") as other:
        for name in ("choregos_session", RELAY_COOKIE):
            assert (
                await other.get("/api/v1/me", headers={**LOOPBACK, "Cookie": f"{name}={value}"})
            ).status_code == 401


async def test_the_session_appears_in_the_accounts_session_list_and_is_audited(
    computer: AsyncClient, phones: Any, owner: str
) -> None:
    phone = phones()
    assert (await _exchange(phone, _code(await _pair(computer)))).status_code == 204
    listed = (await computer.get("/api/v1/me/sessions", headers=LOOPBACK)).json()["items"]
    (paired,) = [row for row in listed if not row["current"]]
    _schema("BrowserSession").validate(paired)
    assert (paired["kind"], paired["channel"]) == ("browser", "pairing")
    assert paired["client_name"] == PHONE[:80]
    assert paired["ip"] == "203.0.113.7"
    on_the_phone = (await phone.get("/api/v1/me/sessions", headers=RELAY)).json()["items"]
    assert [row["id"] for row in on_the_phone if row["current"]] == [paired["id"]]

    from tranibor_api.db.models import AuditLog
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        (entry,) = (
            await session.execute(select(AuditLog).where(AuditLog.action == "device.paired"))
        ).scalars()
    assert entry.target_id == paired["id"]
    assert (entry.payload["client_name"], entry.payload["ip"], entry.payload["user_id"]) == (
        PHONE[:80],
        "203.0.113.7",
        owner,
    )


async def test_the_session_is_ended_from_my_account(computer: AsyncClient, phones: Any) -> None:
    phone = phones()
    assert (await _exchange(phone, _code(await _pair(computer)))).status_code == 204
    listed = (await computer.get("/api/v1/me/sessions", headers=LOOPBACK)).json()["items"]
    (paired,) = [row for row in listed if not row["current"]]
    assert (await computer.delete(f"/api/v1/me/sessions/{paired['id']}", headers=LOOPBACK)).status_code == 204
    assert (await phone.get("/api/v1/me", headers=RELAY)).status_code == 401


async def test_the_session_is_no_fresher_than_the_sign_in_that_created_the_pairing(
    computer: AsyncClient, phones: Any
) -> None:
    from tranibor_api.config import get_settings
    from tranibor_api.security import read_session

    _stale(computer, 8)
    created_by = read_session(computer.cookies["choregos_session"], get_settings())
    assert created_by is not None
    phone = phones()
    assert (await _exchange(phone, _code(await _pair(computer)))).status_code == 204
    payload = read_session(phone.cookies[RELAY_COOKIE], get_settings())
    assert payload is not None
    assert payload["auth_time"] == created_by["auth_time"]
    assert payload["door"] == "relay"
    listed = (await phone.get("/api/v1/me/sessions", headers=RELAY)).json()["items"]
    (mine,) = [row for row in listed if row["current"]]
    stamp = datetime.fromisoformat(mine["auth_time"].replace("Z", "+00:00"))
    assert int((stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()) == created_by["auth_time"]


async def test_a_second_exchange_is_refused(computer: AsyncClient, phones: Any) -> None:
    code = _code(await _pair(computer))
    assert (await _exchange(phones(), code)).status_code == 204
    again = await _exchange(phones(), code)
    assert again.status_code == 401, again.text
    assert not again.headers.get_list("set-cookie")
    (row,) = await _rows()
    assert row.status == "consumed"


async def test_two_exchanges_at_once_open_one_session(computer: AsyncClient, phones: Any) -> None:
    code = _code(await _pair(computer))
    answers = await asyncio.gather(_exchange(phones(), code), _exchange(phones(), code))
    assert sorted(answer.status_code for answer in answers) == [204, 401]

    from tranibor_api.db.models import UserSession
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        paired = (
            await session.execute(
                select(func.count()).select_from(UserSession).where(UserSession.channel == "pairing")
            )
        ).scalar_one()
    assert paired == 1


async def test_after_five_minutes_the_pairing_is_refused(computer: AsyncClient, phones: Any) -> None:
    from tranibor_api.db.models import DeviceAuthorization
    from tranibor_api.db.session import session_scope

    code = _code(await _pair(computer))
    async with session_scope(orgs="*") as session:
        await session.execute(update(DeviceAuthorization).values(expires_at=utcnow() - timedelta(seconds=1)))
    late = await _exchange(phones(), code)
    assert late.status_code == 401, late.text
    assert not late.headers.get_list("set-cookie")


async def test_an_unknown_code_is_refused_without_saying_why(computer: AsyncClient, phones: Any) -> None:
    await _pair(computer)
    refused = await _exchange(phones(), "x" * 43)
    assert refused.status_code == 401, refused.text
    assert refused.json()["detail"] == "this pairing link is not valid: show a new one on the computer"


async def test_the_exchange_on_the_loopback_door_is_refused(computer: AsyncClient, phones: Any) -> None:
    code = _code(await _pair(computer))
    refused = await _exchange(phones(), code, LOOPBACK)
    assert refused.status_code == 404, refused.text
    assert not refused.headers.get_list("set-cookie")
    # Nothing was taken: the relay door still exchanges it.
    assert (await _exchange(phones(), code)).status_code == 204


async def test_a_pairing_is_not_a_device_code_a_surface_may_poll(computer: AsyncClient, phones: Any) -> None:
    """The device flow's token route never hands out a pairing: its kind is the computer's alone."""
    code = _code(await _pair(computer))
    polled = await phones().post(
        "/api/v1/auth/device/token",
        json={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": code},
        headers=RELAY,
    )
    assert polled.status_code == 400, polled.text
    assert polled.json()["error"] == "invalid_grant"
    assert (await _exchange(phones(), code)).status_code == 204


async def test_the_relay_door_names_device_pairing(computer: AsyncClient) -> None:
    methods = await computer.get("/api/v1/auth/methods", headers=RELAY)
    assert methods.json() == {"posture": "personal", "door": "relay", "methods": ["device_pairing"]}
    _schema("AuthMethods").validate(methods.json())


# ───────────────────────── outside the personal posture, neither route exists ─────────────────────────


async def test_outside_the_personal_posture_neither_route_exists(
    client: AsyncClient, org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "true")
    reset_settings_cache()
    await login(client, "admin@northwind.dev")
    # Even with a public URL a pairing link could name: the posture decides, not the address.
    monkeypatch.setenv("TRANIBOR_PUBLIC_URL", RELAY_URL)
    reset_settings_cache()
    for headers in ({}, LOOPBACK, RELAY):
        assert (await client.post(PAIRINGS, headers=headers)).status_code == 404
        assert (await client.post(EXCHANGE, json={"code": "x" * 43}, headers=headers)).status_code == 404
    assert await _rows() == []
