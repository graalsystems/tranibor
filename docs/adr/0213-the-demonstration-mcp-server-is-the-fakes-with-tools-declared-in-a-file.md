# 0213 — The demonstration MCP server is the fakes' server, with its tools declared in a file

- **Status**: accepted, 2026-11-23
- **Concerns**: `packages/adapters` (`fakes/demo_mcp.py`, `builtins._current_fake_mcp`), `apps/api` (`config`,
  the lifespan), `packages/core` (`production.refuse_demo_mcp_tools`); builds on
  [ADR 0005](0005-adapters-and-fakes.md), [ADR 0034](0034-connectors-by-capability.md)
  and [ADR 0212](0212-the-api-can-run-the-workers-inline-for-a-demonstration.md)

## Context

A pre-sales demonstration has to show a **governed write to a business system**: an agent proposes to record
a warranty decision in a dealer system, a person approves, the write lands, a read shows it. Under
`TRANIBOR_FAKES=1` an `mcp` connector is served by the process-wide `FakeMcpServer`, but that server has no
tool until a test scripts it in Python: through the API, discovery returns nothing, so the demonstration
needed a real MCP server running beside the API.

Two designs were considered:

- **A separate server** (`tranibor-demo-mcp`, streamable HTTP on a port). It is "like any MCP server" and
  survives beside any API, but it is a second process to start, a URL to open in the egress allowlist
  (ADR 0066), a credential to decide on, and a network listener that answers anyone: the opposite of what
  must never run outside development.
- **The fakes' server with its tools declared in a file.** Everything a demonstration is about already
  exists on that path and is the product's own: the connector, `discover` (a new tool born closed), the
  operation policies `allowed` and `approval`, the schema check before the call, the governed action and
  its separation of duties, the `connector.call` effect. Only the tools are scripted.

## Decision

1. **`TRANIBOR_DEMO_MCP_TOOLS=<file>` scripts the fakes' MCP server.** When the process-wide server is first
   built (`_current_fake_mcp`), the file, YAML or JSON, declares each tool: `name`, `description`, `kind`
   (`read` is announced `readOnlyHint`, `write` is not), `input_schema`, a canned `answer`, and on a read tool
   `recorded_from`: the write tools whose recorded calls come back under `recorded`, those that share
   every argument the read gives (a claim's id). Unset, nothing changes.
2. **The server records every write** (`DemoMcpServer.writes`: order, tool, arguments, time) and serves them
   back through a read tool, `received_writes` unless the file declares that name, so that the demonstration
   shows what reached the "business system". As ever, a tool is born closed: the administrator opens it.
3. **It holds no credential and is refused elsewhere.** The server checks no token; a file whose schemas or
   answers name `token`, `secret`, `password`, `api_key` or `credential` is refused. Building it is refused
   unless `TRANIBOR_FAKES` is on and `TRANIBOR_ENV` is `dev` or `test` (`refuse_demo_mcp_tools`), in the
   adapter and again at the API's startup (the API reads the file there, so a mistake shows at once).
4. **No new process, no new port, no new crossing.** The adapters already depend on the core; the API
   already imports the adapters.

## Limits

- **In-process**: the server lives in the process that builds it, so the worker that plays the approved action
  must be that process: use it with `TRANIBOR_DEMO_INLINE_WORKER=1` (ADR 0212). With separate worker processes
  each has its own server and records its own writes.
- It records in memory: a restart forgets. It is not an MCP server a client outside the platform can reach.
- Canned answers: no logic beyond matching a read to the writes it recorded.

## Consequences

`docs/integrations.md` ("A demonstration MCP server") says how to run it, with a sample for a dealer system
(`examples/demo-mcp/dealer-system.yaml`). `apps/api/tests/test_demo_mcp_server.py` proves the chain through
the broker, and the refusals.
