"""ADR 0224 (R7): the cookies are `tranibor_*`; a browser that holds a former `choregos_*` cookie is read
under the new name, then the former one is cleared (and the session set again under the new name)."""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tranibor_api.config import get_settings
from tranibor_api.former_cookies import FormerCookiesMiddleware


def _set_cookies(response: Any) -> list[str]:
    return list(response.headers.get_list("set-cookie"))


def _named(cookies: list[str], name: str) -> list[str]:
    return [c for c in cookies if c.startswith(f"{name}=")]


async def test_the_session_cookie_is_tranibor_session_by_default(client: AsyncClient, admin: str) -> None:
    assert get_settings().session_cookie == "tranibor_session"
    assert client.cookies.get("tranibor_session")
    assert not client.cookies.get("choregos_session"), "the former name is never set"


async def test_a_session_held_under_the_former_name_stays_signed_in_and_moves(
    client: AsyncClient, app: Any, admin: str
) -> None:
    value = client.cookies.get("tranibor_session")
    browser = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    browser.cookies.set("choregos_session", value)
    me = await browser.get("/api/v1/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == admin
    sets = _set_cookies(me)
    (cleared,) = _named(sets, "choregos_session")
    assert "max-age=0" in cleared.lower() and "path=/" in cleared.lower()
    (moved,) = _named(sets, "tranibor_session")
    assert moved.startswith(f"tranibor_session={value};") and "httponly" in moved.lower()
    # What the browser then holds, the new cookie only, keeps its session and sets nothing more.
    moved_browser = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    again = await moved_browser.get("/api/v1/me", headers={"cookie": f"tranibor_session={value}"})
    assert again.status_code == 200 and not _set_cookies(again)


async def test_a_former_cookie_beside_the_new_one_is_cleared_and_the_new_one_wins(
    client: AsyncClient, app: Any, admin: str
) -> None:
    value = client.cookies.get("tranibor_session")
    browser = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    browser.cookies.set("tranibor_session", value)
    browser.cookies.set("choregos_session", "stale-and-unsigned")
    me = await browser.get("/api/v1/me")
    assert me.status_code == 200, me.text
    sets = _set_cookies(me)
    assert _named(sets, "choregos_session") and not _named(sets, "tranibor_session")


def _probe(seen: dict[str, str]) -> Starlette:
    async def read(request: Request) -> Response:
        seen.update(request.cookies)
        return JSONResponse({})

    paths = ["/api/v1/auth/callback", "/api/v1/auth/linked-accounts/callback"]
    return Starlette(routes=[Route(path, read) for path in paths])


async def test_a_handshake_begun_before_the_upgrade_finishes_and_its_cookie_is_cleared() -> None:
    seen: dict[str, str] = {}
    app = FormerCookiesMiddleware(_probe(seen))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        oidc = await http.get("/api/v1/auth/callback", headers={"cookie": "choregos_oidc=handshake; other=1"})
        assert seen["tranibor_oidc"] == "handshake" and seen["other"] == "1"
        sets = _set_cookies(oidc)
        assert _named(sets, "choregos_oidc") and not _named(sets, "tranibor_oidc"), "a handshake is not kept"

        seen.clear()
        link = await http.get(
            "/api/v1/auth/linked-accounts/callback", headers={"cookie": "choregos_forge_link=verifier"}
        )
        assert seen["tranibor_forge_link"] == "verifier"
        (cleared,) = _named(_set_cookies(link), "choregos_forge_link")
        assert "path=/api/v1/auth/linked-accounts" in cleared.lower(), "cleared on the path it was set on"


async def test_a_request_without_a_former_cookie_is_left_as_it_is() -> None:
    seen: dict[str, str] = {}
    app = FormerCookiesMiddleware(_probe(seen))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        answer = await http.get("/api/v1/auth/callback", headers={"cookie": "tranibor_oidc=h"})
    assert seen == {"tranibor_oidc": "h"} and not _set_cookies(answer)
