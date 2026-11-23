# SPDX-License-Identifier: Apache-2.0
"""The demonstration MCP server (ADR 0213): the fakes' MCP server, whose tools are declared in a file.

A pre-sales demonstration has to show a governed write to a business system (a warranty decision
recorded in a dealer system, say) without running a real MCP server. Under `TRANIBOR_FAKES=1` every
`mcp` connector is served by the process-wide `FakeMcpServer`, which offers no tool until a test scripts
it. Setting `TRANIBOR_DEMO_MCP_TOOLS=<file>` scripts it from a YAML (or JSON) file instead:

    tools:
      - name: record_warranty_decision
        description: Records the decision taken on a warranty claim.
        kind: write                      # `read` or `write`: a read is announced `readOnlyHint`
        input_schema: {type: object, properties: {claim_id: {type: string}}, required: [claim_id]}
        answer: {status: recorded}       # the canned answer; defaults to {"ok": true}
      - name: get_claim
        description: The claim, with the decisions recorded on it.
        kind: read
        input_schema: {...}
        answer: {claim_id: C-1, status: open}
        recorded_from: [record_warranty_decision]   # adds `recorded`: the writes received for this claim

The server is the real MCP machinery (discovery, the broker, the governed action, the operation
policies, the schema checks): only the tools are scripted. It RECORDS every write it receives (`writes`)
and serves them back through a read tool, `received_writes` unless the file declares a tool of that
name, so that the demonstration shows what reached the "business system".

It is the fakes' own: it checks no credential, holds none, and building it is refused unless the fakes
are on and `TRANIBOR_ENV` is `dev` or `test` (`refuse_demo_mcp_tools`).
"""

from __future__ import annotations

import copy
import datetime
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml
from tranibor_core.production import (
    DEMO_MCP_TOOLS_VARIABLE,
    ENVIRONMENT_VARIABLE,
    FAKES_VARIABLE,
    refuse_demo_mcp_tools,
)

from ..errors import ConfigurationError
from .mcp import FakeMcpServer

#: The read tool that lists every write received, unless the file declares one of that name.
RECEIVED_WRITES = "received_writes"
#: A demonstration lists its tools on one page.
PAGE_SIZE = 200
_KINDS = ("read", "write")
_KEYS = {"name", "description", "kind", "input_schema", "answer", "recorded_from"}
#: What a demonstration file must not carry: this server holds no credential, ever.
_SECRET_WORDS = ("token", "secret", "password", "api_key", "apikey", "credential")


@dataclass
class DemoMcpServer(FakeMcpServer):
    """A `FakeMcpServer` that records the writes it receives."""

    #: every write received, in order: {"n", "tool", "arguments", "received_at"}
    writes: list[dict[str, Any]] = field(default_factory=list)

    def record(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "n": len(self.writes) + 1,
            "tool": tool,
            "arguments": copy.deepcopy(arguments),
            "received_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        }
        self.writes.append(entry)
        return entry


def _refuse(path: str, message: str) -> ConfigurationError:
    return ConfigurationError(f"{DEMO_MCP_TOOLS_VARIABLE} ({path}): {message}")


def _has_secret_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if any(word in str(key).lower() for word in _SECRET_WORDS):
                return str(key)
            found = _has_secret_key(inner)
            if found:
                return found
    elif isinstance(value, list):
        for inner in value:
            found = _has_secret_key(inner)
            if found:
                return found
    return None


def _declared(path: pathlib.Path) -> list[dict[str, Any]]:
    try:
        document = yaml.safe_load(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise _refuse(str(path), f"cannot be read: {error}") from error
    tools = document.get("tools") if isinstance(document, dict) else None
    if not isinstance(tools, list) or not tools:
        raise _refuse(str(path), "must hold a non-empty `tools:` list")
    names: set[str] = set()
    for index, tool in enumerate(tools, 1):
        where = f"tool {index}"
        if not isinstance(tool, dict):
            raise _refuse(str(path), f"{where} must be a mapping")
        if set(tool) - _KEYS:
            raise _refuse(
                str(path), f"{where} has unknown keys {sorted(set(tool) - _KEYS)}; known: {sorted(_KEYS)}"
            )
        name = tool.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise _refuse(str(path), f"{where} needs a `name`, unique in the file")
        names.add(name)
        if tool.get("kind") not in _KINDS:
            raise _refuse(str(path), f"tool `{name}` needs `kind: read` or `kind: write`")
        schema = tool.get("input_schema", {"type": "object"})
        answer = tool.get("answer", {"ok": True})
        if not isinstance(schema, dict) or not isinstance(answer, dict):
            raise _refuse(str(path), f"tool `{name}`: `input_schema` and `answer` are mappings")
        recorded = tool.get("recorded_from", [])
        if not isinstance(recorded, list) or (recorded and tool["kind"] != "read"):
            raise _refuse(str(path), f"tool `{name}`: `recorded_from` is a list, on a read tool")
        leaked = _has_secret_key(schema) or _has_secret_key(answer)
        if leaked:
            raise _refuse(str(path), f"tool `{name}` names `{leaked}`: this server holds no credential")
    writes = {t["name"] for t in tools if t["kind"] == "write"}
    for tool in tools:
        unknown = [w for w in tool.get("recorded_from", []) if w not in writes]
        if unknown:
            raise _refuse(str(path), f"tool `{tool['name']}` reads {unknown}, which are not write tools")
    return [dict(t) for t in tools]


def _matches(read: dict[str, Any], write: dict[str, Any]) -> bool:
    """A write belongs to a read when every argument they share is equal."""
    return all(write.get(key) == value for key, value in read.items() if key in write)


def load_demo_mcp(path: str | os.PathLike[str]) -> DemoMcpServer:
    """The demonstration server of a tools file; refused outside development under the fakes."""
    refuse_demo_mcp_tools(
        environment=os.environ.get(ENVIRONMENT_VARIABLE, "dev"), fakes=os.environ.get(FAKES_VARIABLE)
    )
    declared = _declared(pathlib.Path(path))
    server = DemoMcpServer(page_size=PAGE_SIZE)
    for tool in declared:
        read = tool["kind"] == "read"
        server.tools.append(
            {
                "name": tool["name"],
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("input_schema", {"type": "object"}),
                **({"annotations": {"readOnlyHint": True}} if read else {}),
            }
        )
        server.gestures[tool["name"]] = _gesture(server, tool)
    if RECEIVED_WRITES not in server.gestures:
        server.tools.append(
            {
                "name": RECEIVED_WRITES,
                "description": "Every write this demonstration server received, in order.",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            }
        )
        server.gestures[RECEIVED_WRITES] = lambda _arguments: {"writes": copy.deepcopy(server.writes)}
    return server


def _gesture(server: DemoMcpServer, tool: dict[str, Any]) -> Any:
    name = str(tool["name"])
    answer = dict(tool.get("answer", {"ok": True}))
    sources = list(tool.get("recorded_from", []))
    if tool["kind"] == "write":

        def write(arguments: dict[str, Any]) -> dict[str, Any]:
            entry = server.record(name, arguments)
            return {**copy.deepcopy(answer), "recorded": entry["n"]}

        return write

    def read(arguments: dict[str, Any]) -> dict[str, Any]:
        produced = copy.deepcopy(answer)
        if sources:
            produced["recorded"] = [
                copy.deepcopy(w["arguments"])
                for w in server.writes
                if w["tool"] in sources and _matches(arguments, w["arguments"])
            ]
        return produced

    return read


def demo_mcp_tools_path() -> str:
    """The file `TRANIBOR_DEMO_MCP_TOOLS` names; empty when the demonstration server is not asked for."""
    return os.environ.get(DEMO_MCP_TOOLS_VARIABLE, "").strip()


__all__ = ["RECEIVED_WRITES", "DemoMcpServer", "demo_mcp_tools_path", "load_demo_mcp"]
