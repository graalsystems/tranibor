# SPDX-License-Identifier: Apache-2.0
"""The run's API refuses a request that carries a model subscription credential (ADR 0227).

`/api/v1/internal/*` is where runners, session pods and the platform's proxies send what an agent
produced: events, results, questions, tool calls. A personal runner scrubs every body it sends by the
exact value of its owner's token (S87-21); this guard is the platform's own wall behind it. A request
under the prefix whose URL or body holds a subscription shape (`tranibor_core.subscription_credentials`) is
answered 422 `subscription_credential_refused` before any route reads it: nothing of it is stored,
journaled or forwarded, and the answer names the shape, never the value. A refusal is loud on purpose:
it means a runner let a credential through, and silently rewriting the body would hide that.

The URL of every request is read; the body of the methods that carry one (POST, PUT, PATCH). A body
declared or found over `MAX_SCANNED_BYTES` is served unread and logged `subscription_ingress.unscanned`
with its size: an artifact upload can be large, and the runner's scrub covers it. A read body is
buffered once and handed to the route unchanged. A refused URL is not logged: it holds the credential.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tranibor_core.subscription_credentials import REFUSAL_CODE, SubscriptionCredentialRefusedError, shapes_in

from .errors import PROBLEM_JSON
from .logging import get_logger

#: Everything a run, a session pod or a platform proxy calls with its own token.
PREFIX = "/api/v1/internal"
#: The methods whose body is read.
METHODS = frozenset({"POST", "PUT", "PATCH"})
#: The largest body read; a larger one is served unread and logged.
MAX_SCANNED_BYTES = 16 * 1024 * 1024

logger = get_logger("choregos.subscription_ingress")


class SubscriptionIngress:
    """ASGI middleware: the wall of the run's API against a subscription credential."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or not (path == PREFIX or path.startswith(PREFIX + "/")):
            await self.app(scope, receive, send)
            return
        # The URL first: a token in a path or a query string is refused, and the path is then not logged.
        query = bytes(scope.get("query_string", b"")).decode("latin-1")
        labels = shapes_in(f"{path}?{query}")
        if labels:
            logger.error(
                "subscription_ingress.refused", path="(withheld: it holds the credential)", shapes=labels
            )
            await _refuse(send, PREFIX, labels)
            return
        if scope.get("method") not in METHODS:
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_SCANNED_BYTES:
            logger.warning("subscription_ingress.unscanned", path=_route(path), size=int(declared))
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        size = 0
        body = bytearray()
        more = True
        while more:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size <= MAX_SCANNED_BYTES:
                body.extend(chunk)
            more = bool(message.get("more_body", False))
        if size > MAX_SCANNED_BYTES:
            logger.warning("subscription_ingress.unscanned", path=_route(path), size=size)
        else:
            # Latin-1 maps every byte to one character: the shapes are ASCII, and no body can fail to decode.
            labels = shapes_in(body.decode("latin-1"))
            if labels:
                logger.error("subscription_ingress.refused", path=_route(path), shapes=labels)
                await _refuse(send, path, labels)
                return
        replay = iter(messages)

        async def replayed() -> Message:
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await self.app(scope, replayed, send)


def _route(path: str) -> str:
    """The path with its identifiers kept: they are run and session ids, never secrets."""
    return path[:200]


async def _refuse(send: Send, path: str, labels: list[str]) -> None:
    import json

    error = SubscriptionCredentialRefusedError("this request to the run's API", labels)
    problem = {
        "type": f"https://choregos.dev/problems/{REFUSAL_CODE}",
        "title": "Subscription credential refused",
        "status": 422,
        "detail": str(error),
        "instance": path,
        "code": REFUSAL_CODE,
        "shapes": labels,
    }
    payload = json.dumps(problem).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 422,
            "headers": [
                (b"content-type", PROBLEM_JSON.encode()),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def install_subscription_ingress(app: object) -> None:
    """Add the wall to the API's middlewares (`app.add_middleware`)."""
    app.add_middleware(SubscriptionIngress)  # type: ignore[attr-defined]
