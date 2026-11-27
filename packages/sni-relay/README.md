# tranibor-sni-relay

The relay a personal installation dials out to, and the installation's side of it
([ADR 0230](../../docs/adr/0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md)).
It is built against `packages/contracts/relay.yaml` (`/relay/v1`, protocol 1) and nothing else of the
platform: it imports no code of the API, the orchestrator or the runner, and nothing of the core
imports it.

This page is the package's own. What the relay sees and keeps, and how to run one, is the operator's
page (`docs/relay.md`); the operator's procedures are `docs/runbooks/relay-operations.md`.

## Running it

`choregos-relay serve` runs the relay on the configuration its environment gives (`TRANIBOR_RELAY_*`,
listed in `server.py`), until SIGTERM; `choregos-relay check` says what it would serve. Its image is
`docker/relay.Dockerfile`, its chart `charts/choregos-relay` (S87-44).

## What the relay does

- **It routes by SNI and never terminates TLS.** The SNI listener reads a phone's ClientHello within
  16 KiB and 5 seconds (`sni.py`), and routes `<id>.<relay-domain>` to that installation's one control
  session. The relay holds no certificate and no key for any `<id>` name: its configuration has no
  field for one, and the SNI listener is opened without TLS.
- **It never names a destination.** For each phone connection it sends the installation's agent an
  order to dial back that names a stream, a single-use ticket and the phone's address, and no host or
  port. The agent dials back to the relay's own host with the ticket; the relay then splices the
  ClientHello and every byte after it, both ways, unparsed.
- **It closes what it does not route.** No server name, a malformed or out-of-bounds ClientHello, a
  name outside the domain, an unknown or suspended id, no control session, a limit passed: the
  connection is closed, and nothing is stored.
- **It trusts a signature, not a connection.** The control hello is a compact JWS by the
  installation's Ed25519 key over the relay's nonce, with an `iat` within 60 seconds. A new session
  replaces the live one only when its hello verifies.
- **It draws the ids.** Pairing is by device code, bound to a relay account; the id is 60 random bits
  written as 12 base32 characters, and a pairing request that names one is refused.
- **It keeps one metadata line per connection, and nothing else.** Each line is validated against
  `relay-metadata-line.schema.json` before it is written; the retention is the operator's, and the
  relay refuses to start without one.

## What is not here yet

- The full agent, its configuration and its certificate by TLS-ALPN-01, and `choregos local relay`
  (S87-43). `agent.py` holds the part of the agent the relay is proven against.
- A persistent registry: `RelayRegistry` keeps accounts' installations and pairings in memory.
- An account page and an approval door: nothing serves the verification URI yet.
- An operator interface to suspend, resume or remove an id (`Relay.suspend` is a call in the process).

## Tests

- `tests/test_passthrough.py`: a TLS client through an in-process relay reaches a TLS server behind an
  in-process agent and sees that server's certificate; the relay holds no private key for the name;
  every refused case is closed; tickets, hellos, pairing, limits, suspension and the metadata lines.
- `tests/test_sni.py`: the parser never raises on arbitrary bytes and never reads past its bound.
- `tests/test_server.py`: the command reads the environment the chart writes, refuses what the relay
  would refuse, serves until SIGTERM and writes nothing about a connection.
