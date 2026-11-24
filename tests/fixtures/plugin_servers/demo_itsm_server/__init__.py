# SPDX-License-Identifier: Apache-2.0
"""A minimal MCP server for tests, standing for a plugin's own Python package (ADR 0218).

`settings_from_env` and `build_app` are what the operator's `TRANIBOR_DEMO_PLUGIN_SERVERS` file names.
It answers MCP over JSON (`initialize`, `tools/list`, `tools/call`) behind the platform's bearer token,
and serves its fake only: `DEMO_ITSM_FAKE=1` (a real instance is out of the question here). An optional
seed (`DEMO_ITSM_SEED`, a JSON file of records) replaces the built-in records.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

RECORDS = [{"number": "INC0000001", "short_description": "VPN is down"}]
TOOLS = [
    {"name": "search_records", "description": "Records.", "inputSchema": {"type": "object"}},
    {"name": "add_work_note", "description": "A note.", "inputSchema": {"type": "object"}},
]


@dataclass
class Settings:
    token: str
    seed: str | None = None


def settings_from_env(env: Mapping[str, str]) -> Settings:
    if env.get("DEMO_ITSM_FAKE") != "1":
        raise ValueError("this server runs only on its fake (DEMO_ITSM_FAKE=1)")
    if not env.get("TRANIBOR_SERVER_TOKEN"):
        raise ValueError("TRANIBOR_SERVER_TOKEN is not set")
    return Settings(env["TRANIBOR_SERVER_TOKEN"], env.get("DEMO_ITSM_SEED") or None)


def build_app(settings: Settings) -> Starlette:
    records = list(RECORDS)
    if settings.seed:
        with open(settings.seed, encoding="utf-8") as seed:
            records = json.load(seed)
    notes: list[dict[str, Any]] = []

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        app.state.started = True  # a call before the lifespan ran is refused below
        yield

    async def mcp(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != f"Bearer {settings.token}":
            return JSONResponse({"error": "the platform's bearer token is required"}, status_code=401)
        if not getattr(request.app.state, "started", False):
            return JSONResponse({"error": "lifespan not run"}, status_code=500)
        body = await request.json()
        method = body.get("method")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "demo-itsm"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = body["params"]
            arguments = params.get("arguments") or {}
            if params["name"] == "search_records":
                payload: dict[str, Any] = {"records": records, "notes": notes}
            else:
                notes.append(arguments)
                payload = {"recorded": len(notes)}
            result = {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
            }
        else:
            return JSONResponse({}, status_code=202)
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST"])], lifespan=lifespan)
