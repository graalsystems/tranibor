# SPDX-License-Identifier: Apache-2.0
"""A personal installation's two doors, named by the edge, and a session cookie per door (ADR 0228 §5, §6).

A personal installation is reached by two doors: the loopback door, the computer it runs on
(`http://127.0.0.1`), and the relay door, a phone through the relay of ADR 0230. The edge (Caddy, in
`deploy/personal/`) listens once per door and writes `X-Choregos-Door: loopback` or `relay` from the
LISTENER a request came through, over whatever the client sent. The door is never read from `Host`: a
client through the relay chooses its `Host`, and `Host: 127.0.0.1` would otherwise be the computer.

What holds in the personal posture (`tranibor_core.production.is_personal`), and only there:

- **No door, no answer.** A request without exactly one known door is misdirected: 421, before anything
  routes. Only the edge and the platform's own containers reach the API, and the probes are not behind the
  edge: `/healthz` and `/readyz` answer without a door.
- **The relay door does not reach the machines' routes.** The outposts' door (`/outposts`, ADR 0148) and the
  run's API (`/api/v1/internal`, ADR 0144) answer 404 on the relay door, as an unknown route does; the edge
  does not route them there either.
- **A cookie per door.** On the loopback door the session cookie is `choregos_session`, without `Secure`
  (the door is plain http on loopback); on the relay door it is `__Host-choregos_session`, `Secure`, host-only
  on `/`. A session's signed claims name the door that issued it, and a cookie is read on that door only: a
  loopback cookie presented through the relay, under either name, opens nothing.

Outside the personal posture there are no doors: the header is ignored, the cookie is
`TRANIBOR_SESSION_COOKIE` with `Secure` as `Settings.cookies_secure` says, and nothing here answers.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Response
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose
from tranibor_core.production import is_personal

from .config import Settings, get_settings
from .errors import PROBLEM_JSON
from .logging import get_logger
from .security import read_session

#: The header the edge writes from its listener, replacing the client's.
HEADER = "x-choregos-door"
LOOPBACK: Literal["loopback"] = "loopback"
RELAY: Literal["relay"] = "relay"
Door = Literal["loopback", "relay"]
#: Each door's session cookie. `__Host-` makes a browser refuse it without `Secure`, `Path=/` and a `Domain`.
LOOPBACK_COOKIE = "choregos_session"
RELAY_COOKIE = "__Host-choregos_session"
#: The routes answered without a door: the probes of the platform's own supervisor.
DOORLESS = frozenset({"/healthz", "/readyz"})
#: What only the loopback door reaches: the outposts' door and the run's API, called by machines.
LOOPBACK_ONLY = ("/outposts", "/api/v1/internal")
#: How many characters of a path a refusal's log line keeps.
LOGGED_PATH_CHARS = 80

logger = get_logger("choregos.doors")


def door_named(headers: Headers) -> Door | None:
    """The door one `X-Choregos-Door` line names; `None` for no line, two lines or an unknown value."""
    lines = headers.getlist(HEADER)
    if len(lines) != 1:
        return None
    value = lines[0].strip().lower()
    if value == LOOPBACK:
        return LOOPBACK
    if value == RELAY:
        return RELAY
    return None


def door_of(connection: HTTPConnection, settings: Settings) -> Door | None:
    """The door the edge named for this request, in the personal posture; `None` outside it (no doors)."""
    if not is_personal(settings.env):
        return None
    return door_named(connection.headers)


def reaches(door: str, path: str) -> bool:
    """Whether a request on `door` may reach `path`: the relay door reaches neither machines' route."""
    if door != RELAY:
        return True
    return not any(path == prefix or path.startswith(f"{prefix}/") for prefix in LOOPBACK_ONLY)


def _cookie(connection: HTTPConnection | None, settings: Settings) -> tuple[str, bool]:
    """The session cookie's name and its `Secure` flag for a request."""
    if connection is None or not is_personal(settings.env):
        return settings.session_cookie, settings.cookies_secure
    if door_named(connection.headers) == RELAY:
        return RELAY_COOKIE, True
    return LOOPBACK_COOKIE, False


def session_cookie_names(settings: Settings) -> tuple[str, ...]:
    """Every name a session cookie may have here: what the origin guard looks for."""
    if is_personal(settings.env):
        return (LOOPBACK_COOKIE, RELAY_COOKIE)
    return (settings.session_cookie,)


def door_claims(connection: HTTPConnection | None, settings: Settings) -> dict[str, str]:
    """What a new session's signed claims say of the door: the one that issues it, in the personal posture."""
    door = door_of(connection, settings) if connection is not None else None
    return {"door": door} if door is not None else {}


def read_session_cookie(connection: HTTPConnection, settings: Settings) -> dict[str, Any] | None:
    """The signed session this request's cookie carries, when it was issued for the request's door."""
    if not is_personal(settings.env):
        return read_session(connection.cookies.get(settings.session_cookie, ""), settings)
    door = door_named(connection.headers)
    if door is None:
        return None
    name, _ = _cookie(connection, settings)
    payload = read_session(connection.cookies.get(name, ""), settings)
    if payload is None or payload.get("door") != door:
        return None
    return payload


def set_session_cookie(
    response: Response, connection: HTTPConnection | None, settings: Settings, value: str, *, max_age: int
) -> None:
    """Set the session cookie of the request's door. No `Domain`: the cookie is the answering host's only."""
    name, secure = _cookie(connection, settings)
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", secure=secure)


def clear_session_cookie(response: Response, connection: HTTPConnection, settings: Settings) -> None:
    """Clear the session cookie of the request's door, with the attributes a browser requires to drop it."""
    if not is_personal(settings.env):
        response.delete_cookie(settings.session_cookie)
        return
    name, secure = _cookie(connection, settings)
    response.delete_cookie(name, httponly=True, samesite="lax", secure=secure)


def _misdirected(path: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": "Misdirected Request",
            "status": 421,
            "detail": "this personal installation answers through its edge only, which names the door a "
            "request came through (`X-Choregos-Door`: `loopback` or `relay`)",
            "instance": path,
        },
        status_code=421,
        media_type=PROBLEM_JSON,
    )


class DoorGuard:
    """In the personal posture: no door, 421; the relay door on a machines' route, 404."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or not is_personal(get_settings().env):
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        if path in DOORLESS:
            await self.app(scope, receive, send)
            return
        door = door_named(Headers(scope=scope))
        if door is not None and reaches(door, path):
            await self.app(scope, receive, send)
            return
        logger.warning("door.refused", door=door, path=path[:LOGGED_PATH_CHARS])
        if scope["type"] == "websocket":
            await WebSocketClose()(scope, receive, send)
            return
        if door is None:
            await _misdirected(path)(scope, receive, send)
            return
        # What the router answers for a path it does not know, byte for byte.
        await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)


def install_door_guard(app: FastAPI) -> None:
    app.add_middleware(DoorGuard)
