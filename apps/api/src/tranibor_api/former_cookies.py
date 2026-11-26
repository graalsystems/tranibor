# SPDX-License-Identifier: Apache-2.0
"""The cookies' former names, still read for the window of ADR 0226 (R7).

The API sets its cookies under `tranibor_*` (`tranibor_session`, `tranibor_oidc`, `tranibor_saml`,
`tranibor_forge_link`, `tranibor_consent`, `tranibor_remote_oauth`). A browser signed in before the
upgrade still holds `choregos_*`. This middleware, before anything reads a cookie, copies a former cookie
under its new name into the request when the new one is absent, so every reader (`request.cookies`,
`Cookie(alias=...)`) knows only the new name. On the way out it clears the former cookie (with the path
it was set on) and, for the session, sets it again under the new name: a person signed in before the
upgrade stays signed in and never holds both. A handshake cookie (OIDC, SAML, a forge link, a consent, a
remote OAuth) lives minutes: it is read under its former name to finish a handshake begun before the
upgrade, then cleared, never set again.

A session cookie name an operator chose (`TRANIBOR_SESSION_COOKIE`) is theirs: only the default name
is renamed. The former names stop being read in the minor that closes the window (0.32.0), in its own PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.requests import cookie_parser
from tranibor_contracts import wire

from .config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class Renamed:
    """One cookie: its new name, its former one, the path it is set on, and whether it is set again."""

    name: str
    path: str = "/"
    #: The session: set again under the new name. A handshake: read once, then cleared.
    keep: bool = False

    @property
    def new(self) -> str:
        return wire.cookie(self.name)

    @property
    def former(self) -> str:
        return wire.former_cookie(self.name)


#: The handshake cookies and the path each is set on (the routers' `set_cookie` calls).
HANDSHAKES = (
    Renamed("oidc"),
    Renamed("saml"),
    Renamed("remote_oauth"),
    Renamed("forge_link", path="/api/v1/auth/linked-accounts"),
    Renamed("consent", path="/api/v1/me/connected-accounts"),
)
SESSION = Renamed("session", keep=True)


def renamed(settings: Settings) -> tuple[Renamed, ...]:
    """The cookies this deployment renames: the session only under its default name."""
    return (*HANDSHAKES, SESSION) if settings.session_cookie == SESSION.new else HANDSHAKES


def _cookies(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """The request's cookies, read as Starlette reads them (its first `Cookie` header)."""
    for key, value in headers:
        if key.lower() == b"cookie":
            return cookie_parser(value.decode("latin-1"))
    return {}


def _set_cookie(name: str, value: str, *, path: str, max_age: int, settings: Settings | None) -> bytes:
    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "HttpOnly", "SameSite=lax"]
    if max_age == 0:
        parts.insert(1, 'expires="Thu, 01 Jan 1970 00:00:00 GMT"')
    if settings is not None and settings.cookies_secure:
        parts.append("Secure")
    return "; ".join(parts).encode("latin-1")


def _sets(headers: list[tuple[bytes, bytes]], name: str) -> bool:
    prefix = f"{name}=".encode("latin-1")
    return any(k.lower() == b"set-cookie" and v.lstrip().startswith(prefix) for k, v in headers)


class FormerCookiesMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        held = _cookies(scope.get("headers", []))
        moved = [c for c in renamed(settings) if held.get(c.former) and not held.get(c.new)]
        stale = [c for c in renamed(settings) if c.former in held]
        if not stale:
            await self.app(scope, receive, send)
            return
        if moved:
            # The new names are appended to the first `Cookie` header, the one every reader parses.
            added = "; ".join(f"{c.new}={held[c.former]}" for c in moved).encode("latin-1")
            headers: list[tuple[bytes, bytes]] = []
            done = False
            for key, value in scope["headers"]:
                first = key.lower() == b"cookie" and not done
                headers.append((key, value + b"; " + added if first else value))
                done = done or first
            scope = {**scope, "headers": headers}

        async def send_clearing(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                for c in stale:
                    raw.append(
                        (b"set-cookie", _set_cookie(c.former, '""', path=c.path, max_age=0, settings=None))
                    )
                    if c.keep and c in moved and not _sets(raw, c.new):
                        raw.append(
                            (
                                b"set-cookie",
                                _set_cookie(
                                    c.new,
                                    held[c.former],
                                    path=c.path,
                                    max_age=settings.session_max_age_s,
                                    settings=settings,
                                ),
                            )
                        )
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_clearing)
