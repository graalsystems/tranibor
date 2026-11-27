# SPDX-License-Identifier: Apache-2.0
"""The SNI relay and the installation's relay agent (ADR 0230): `/relay/v1`, protocol 1.

A personal installation publishes one port, on `127.0.0.1` (ADR 0228 §7). To reach it from a phone,
its relay agent dials out to a relay, which reads the server name of the phone's ClientHello within
bounds and splices the connection, ClientHello first, to the installation's dial-back. The relay
never terminates that TLS: it holds no certificate and no key for any installation's name, and keeps
one metadata line per connection, nothing else.

- `sni`: the bounded ClientHello reader;
- `passthrough`: the relay server, its two listeners and the splice;
- `control`: the control channel, one signed session per installation;
- `pairing`: the device-code pairing and the relay's registry of installations;
- `metadata`: the one line per connection, and its retention;
- `agent`: the installation's side, minimal here (the full agent and `choregos local relay` follow).

This package imports nothing of the API, the orchestrator or the runner, and nothing of the core
imports it (ADR 0230 §1): it is a program of its own, built against `packages/contracts/relay.yaml`.
"""

from __future__ import annotations

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("tranibor-sni-relay")
except _metadata.PackageNotFoundError:  # a source tree that was not installed
    __version__ = "0.0.0+unknown"
