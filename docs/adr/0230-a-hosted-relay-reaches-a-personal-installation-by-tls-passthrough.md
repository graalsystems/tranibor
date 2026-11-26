# 0230 — A hosted relay reaches a personal installation by TLS passthrough and never terminates its TLS

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends nothing, and
  records why it is not [ADR 0147](0147-an-outpost-only-dials-out.md)'s refused "A generic HTTP relay into the
  outpost's network")
- **Concerns**: a new package `packages/sni-relay` (module `tranibor_sni_relay`: `sni.py`, `passthrough.py`,
  `control.py`, `pairing.py`, `agent.py`); a new contract `packages/contracts/relay.yaml` (`/relay/v1`), its
  schemas `relay-hello`, `relay-open-stream`, `relay-pairing-request`, `relay-pairing-answer` and
  `relay-metadata-line` under `packages/contracts/schemas/`, and their models in a new
  `packages/contracts/src/tranibor_contracts/relay.py`; the `relay` profile of `deploy/personal/compose.yaml`
  and the relay listener of `deploy/personal/Caddyfile` (both new with ADR 0228's bundle);
  `choregos local relay` (a new `packages/cli/src/tranibor_cli/local/relay.py`); the readiness item of
  `apps/api/src/tranibor_api/services/readiness.py`; a new chart `charts/choregos-relay`; two new pages,
  `docs/relay.md` and `docs/runbooks/relay-operations.md`. Builds on [ADR 0010](0010-gates-are-mechanisms.md),
  [ADR 0101](0101-governance-the-platform-does-not-enforce-is-refused.md),
  [ADR 0144](0144-the-run-s-api-answers-only-where-runs-are.md) and
  [ADR 0148](0148-the-outpost-door-is-a-third-door-with-its-own-contract.md) (the uplink's signature, a contract
  of our own). Siblings of the same day: the personal runner
  ([ADR 0226](0226-a-person-s-own-machine-runs-that-person-s-runs.md)), the subscription
  ([ADR 0227](0227-a-person-s-own-subscription-is-used-only-by-the-vendor-cli-on-that-person-s-runner.md)), the
  personal installation ([ADR 0228](0228-a-personal-installation-is-one-person-s-production.md)), the surfaces
  that install it
  ([ADR 0229](0229-surfaces-may-install-and-supervise-a-personal-installation-and-its-runner.md)), Web Push
  ([ADR 0194](0194-a-person-s-devices-are-told-by-web-push.md)); planned for phase B, the chat door (ADR 0231)

## Context

The product owner wants a person to use their own installation from their phone, through a relay GraalSystems
hosts, the installation dialling out (decisions of 2026-11-26). A personal installation publishes one port, the
edge's, on `127.0.0.1` (ADR 0228 §7); a phone away from that machine cannot reach it, and the person has no
public address, no domain and no certificate.

The usual tunnels terminate TLS at their operator: an HTTP reverse proxy of the ngrok kind, or Cloudflare
Tunnel. Whoever terminates sees every session cookie, every passkey assertion, every approval and every page the
person reads. A relay that does that would make its operator a processor of everything the person does with
their agents.

Three things already exist under related names, and this is none of them. `tranibor_api.relay`
([ADR 0091](0091-the-search-engines-reach-the-proxy-through-a-relay.md)) is a forward proxy in the API's image
that carries an organisation's search traffic outward. ADR 0147 refuses "A generic HTTP relay into the outpost's
network", in which the control plane would address hosts inside someone's network, and §11 there says an outpost
has no `relay` capability. ADR 0226 §12 refuses to carry a personal runner's callbacks through a relay. Phase B
also needs a public address: Teams and Notion deliver webhooks; Slack's Socket Mode does not.

## Decision

1. **A package of its own, `packages/sni-relay`, Apache-2.0.** One wheel holds the relay server and the
   installation's agent. It imports nothing of the API, the orchestrator or the runner, and nothing of the core
   depends on it. It is not `tranibor_api.relay`, whose name and purpose stay ADR 0091's. Community edition.
   Held by `packages/sni-relay/tests/test_passthrough.py` (importing the package loads no `tranibor_api`,
   `tranibor_orchestrator` or `tranibor_runner` module).
2. **A versioned contract, `packages/contracts/relay.yaml` (`/relay/v1`).** Modelled on `outpost-door.yaml`
   (ADR 0148 §2): pairing, control and data, with the five schemas above, each closed to extra fields, and
   Strict pydantic models. A change is a `contract-change` (ADR 0001); it lands in S87-08's contract pull
   request. It is not one of [ADR 0168](0168-the-surfaces-frozen-for-1-0.md)'s five frozen surfaces, but
   installations upgrade when their person chooses: the server serves protocol N and N-1, and refuses an older
   hello with a problem naming the minimum, which `choregos local doctor` and the readiness page show. Held by
   `packages/sni-relay/tests/test_passthrough.py` (a hello of N-1 is served; one of N-2 is refused with the
   minimum).
3. **The installation dials out, and only to one place.**
   - *Control.* The agent opens one WSS connection to the relay's own host and signs its hello as the outposts'
     uplink signs a request (`packages/outpost/src/tranibor_outpost/uplink.py`, checked by
     `apps/api/src/tranibor_api/outposts/auth.py`): a compact JWS by the installation's Ed25519 key, binding the
     nonce the relay issued and an `iat` within 60 seconds of the relay's clock. One control session per id; a
     new one replaces the old only on a valid signature.
   - *Data.* For each phone connection the relay sends `relay-open-stream` on the control channel: a stream id,
     a single-use ticket bound to that stream, and the phone's IP address. It has no host and no port field. The
     agent opens a new outbound connection to the relay's host, presents the ticket, and splices what comes back
     to the one address its own configuration names, the edge's relay listener (`edge:8443`), with a PROXY
     protocol v2 header carrying the phone's address. The edge accepts that header on the relay listener only,
     from the agent only.
   - *The port pin holds.* The agent publishes nothing. In the bundle only the edge publishes, on `127.0.0.1`
     (`tests/deploy/test_personal_compose.py::test_only_the_edge_publishes_and_on_loopback`), as every port of
     the development install is on `127.0.0.1` (`tests/dev/test_local_install.py`).

   Held by `packages/sni-relay/tests/test_passthrough.py`: a hello with a bad signature, a stale nonce or an
   `iat` beyond 60 seconds is refused and the live session kept; a ticket used twice or for another stream is
   refused; the agent refuses a `relay-open-stream` holding any field outside the schema and connects nowhere
   but its configured target.
4. **The relay routes by SNI, within bounds, and splices bytes.** It listens on 443 for `*.<relay-domain>` and
   reads the phone's ClientHello with a bounded parser (`sni.py`): 16 KiB and 5 seconds at most. The
   `server_name` must be exactly `<id>.<relay-domain>` with an active id that has a control session. A
   connection without SNI, with a malformed hello, a name outside the domain, an unknown or suspended id, or no
   control session, is closed. A routed connection's bytes, the ClientHello first, are spliced both ways and
   never parsed further. The relay holds no certificate and no private key for any `<id>` name. Its own host
   (pairing, control, data) has a certificate of its own; the phone's TLS records travel inside that connection
   untouched. The relay speaks TCP only, so the edge's relay listener announces no HTTP/3. Held by
   `packages/sni-relay/tests/test_passthrough.py` (a TLS client through an in-process relay sees a certificate
   with the test installation's public key; the relay process holds no private key for the name; every refused
   case is closed) and `packages/sni-relay/tests/test_sni.py` (the parser fuzzed with hypothesis: no exception
   escapes, no read beyond its bound).
5. **TLS ends at the installation's edge, which gets its certificate through the passthrough.** Caddy's relay
   listener obtains `<id>.<relay-domain>` by ACME TLS-ALPN-01: the CA's validation connection carries that SNI
   and the ALPN `acme-tls/1`, the relay routes it like any other, and the edge answers it. The ACME account key
   and the certificate's key are made at the edge and never leave it. The relay's domain needs one wildcard DNS
   record pointing at the relay. DNS-01 brokered by the relay (a TXT record the agent asks for, for its own id
   only, over the control channel) is a fallback for a CA whose validation fails through the passthrough, never
   the default; the key still never leaves the edge. Held by `tests/relay/test_relay_end_to_end.py` (CI job
   `relay-e2e`, the Pebble test CA and a stub DNS): the console is served at `https://<id>.test` through the
   relay with a certificate issued to the edge's key.
6. **Random ids, a relay account, pairing by device code.** `choregos local relay enable` runs the agent's
   pairing: the agent makes its Ed25519 key, asks the relay for a device code, and shows the user code to the
   owner, who enters it in their relay account. The relay then draws an id of 60 random bits, written as 12
   base32 characters, and binds it to the account and the agent's public key. Nobody chooses an id. The command
   writes `TRANIBOR_PUBLIC_URL=https://<id>.<relay-domain>` and recreates the API, the console and the edge;
   `choregos local relay disable` stops the agent and restores the loopback URL. Passkeys are bound to that
   hostname (ADR 0228 §4). Held by `tests/relay/test_relay_end_to_end.py` (pairing, then a session through the
   relay) and `packages/sni-relay/tests/test_passthrough.py` (the relay draws the id; a pairing request that
   names one is refused as an extra field).
7. **The relay keeps metadata lines, and nothing else, for a fixed time.** One line per stream, validated
   against `relay-metadata-line` before it is written: the id, the open and close times, the client's IP
   address, the bytes each way, the duration and the close reason. No ALPN, no cipher list, no byte of the
   stream; the relay never has a header, a path or a body to log. The retention is fixed by the relay's
   operator, stated in `docs/relay.md` and in the hosted instance's terms; the server refuses to start without
   one and deletes older lines. Held by `packages/sni-relay/tests/test_passthrough.py` (every line written
   validates; none holds a byte of a stream; a line past the retention is deleted; no retention, no start) and
   by the canary `tests/e2e/test_subscription_never_reaches_platform.py`, which searches the relay's log
   (ADR 0227 §6).
8. **Limits, suspension and an abuse runbook.** Per installation: concurrent streams, new streams per minute and
   bytes per month; per client IP address: new connections per minute. Past a limit the connection is closed and
   the line says why. A suspended id has its ClientHellos closed and its control hello refused. The runbook
   (`docs/runbooks/relay-operations.md`) covers abuse reports, suspension, key rotation, certificate limits, the
   Public Suffix List and incident response. None of it reads content: the relay cannot. Held by
   `packages/sni-relay/tests/test_passthrough.py` (a limit closes; a suspended id is closed and refused).
9. **No store-and-forward.** When an installation has no control session (the machine sleeps, is offline or
   disabled the relay), the relay closes the connection and keeps nothing. Storing a request would mean reading
   it, so terminating TLS. A sender's webhook then fails and is retried on the sender's schedule; connectors
   catch up by polling once the installation is back (phase B). Held by
   `packages/sni-relay/tests/test_passthrough.py` (no control session: closed, nothing written but the line).
10. **Phase B webhooks use the same hostname and are verified at the installation.** A Teams or Notion delivery
    reaches `https://<id>.<relay-domain>/api/v1/webhooks/...`; webhook paths are public on the relay listener
    (ADR 0228 §6). The bytes the vendor signed reach the edge unchanged, since the relay cannot alter them
    without breaking TLS, and the signature is checked at the installation (ADR 0231, planned for phase B). The
    relay holds no vendor secret and no signing key. A readiness item, "reachable from the internet", probes the
    installation through its own relay hostname. Held by `tests/relay/test_webhook_through_relay.py` (S87-70: a
    signed fake Slack event through the relay is verified by HMAC at the installation; the relay's log holds no
    body).
11. **The relay never carries the runner, the run's API or the outposts' door.** On the relay listener,
    `/api/v1/internal/*` and `/outposts/*` answer 404, decided by the listener and never by `Host`, which a
    relay client chooses (ADR 0228 §6), at the edge and again at the API for a request whose door is `relay`; so
    does `/auth/owner`. A personal runner calls the run door on loopback and on the `runs` network (ADR 0226
    §12). Web Push goes from the installation to the push service, never through the relay (ADR 0194). Held by
    `tests/deploy/test_personal_compose.py::test_the_edge_routes_per_listener`,
    `apps/api/tests/test_owner_sign_in.py` (a `relay` door gets 404 on those paths) and
    `tests/relay/test_relay_end_to_end.py` (internal paths answer 404 through a real relay).
12. **Anyone may run a relay; GraalSystems runs the default one, elsewhere.** `charts/choregos-relay` deploys a
    passthrough listener on the wildcard name, the relay's own host and a database of accounts and
    installations, with no secret in its values. The agent's relay URL is a setting whose default names the
    hosted instance. GraalSystems operates that instance from an operations repository that is neither the core
    nor the enterprise edition; nothing in the core depends on it, and a self-hosted relay is a peer of it. Held
    by `tests/charts/test_relay_chart.py` (renders; no secret literal in values; the 443 Service carries no
    TLS-termination annotation).

## Why this is not ADR 0147's refused relay

ADR 0147, under "What is refused, and why": "A generic HTTP relay into the outpost's network. It is a tunnel:
the platform would address hosts, not operations, and ADR 0105 §1 refuses a passthrough for that reason." This
relay differs on each count:

- **One fixed destination, chosen by the installation.** The agent connects only to its own edge's relay
  listener, named in its own configuration.
- **No host addressing.** `relay-open-stream` has no host or port field, and the agent refuses one: neither the
  relay nor the control plane can name a destination.
- **TLS end to end.** The relay sees a ClientHello and encrypted records, never HTTP.
- **The direction of what it carries.** It brings a person's clients to that person's installation; it never
  carries a control plane into a network. It routes no MCP traffic and selects no third-party server, so
  ADR 0105 §1 is not touched either. ADR 0147 §11 holds: an outpost still has no `relay` capability.

## What is refused, and what is only recorded

| Case | Answer | Where |
|:--|:--|:--|
| A ClientHello without SNI, malformed, over 16 KiB or 5 s, outside the domain, of an unknown or suspended id | closed | `sni.py`, `passthrough.py` |
| A control hello badly signed, with a stale nonce, an `iat` beyond 60 s, or older than N-1 | refused; the live session kept | `control.py` |
| A data dial-back with a used, unknown or another stream's ticket | refused | `passthrough.py` |
| A `relay-open-stream` with a host, a port or any other extra field | refused by the agent | `agent.py`, the schema |
| A connection past a limit, per installation or per client IP address | closed, the reason on its line | `passthrough.py` |
| A phone connection while the installation has no control session | closed, nothing stored | `passthrough.py` |
| `/api/v1/internal/*`, `/outposts/*`, `/auth/owner` through the relay | 404, at the edge and at the API | `Caddyfile`, the API's door check |
| Each stream | recorded: one metadata line, kept for the fixed retention | the relay's log |
| A pairing, a suspension | recorded: account, id, agent key, time; the suspension's reason | the relay's database |

Recorded, and not prevented:

- **Whoever routes a name can obtain a certificate for it.** The relay's operator, by TLS-ALPN-01 or DNS, could
  get one for `<id>.<relay-domain>` and stand in the middle. Browsers cannot pin. It is to be detected, not
  prevented: a later story watches Certificate Transparency for a certificate on the name whose key is not the
  edge's, before the hosted instance opens to the public. Until then it is neither. The Android app (ADR 0193)
  may pin the installation's key at pairing.
- **What any network on the path sees.** The ClientHello in clear (SNI, ALPN), the client's address, and the
  sizes and timing of encrypted records. The relay logs only the line of (7).
- **The Public Suffix List and certificate limits.** Every id shares one registered domain. Let's Encrypt limits
  new certificates per registered domain per week, and a phishing page on one id can get the whole domain
  blocklisted, unless the domain is on the Public Suffix List (inclusion takes weeks to months) or the operator
  obtains a limit adjustment. A prerequisite of the hosted instance's opening to the public; the current figures
  are not verified here. Until then another id's page can set cookies for the whole domain; the relay door's
  session cookie is `__Host-` (ADR 0228 §5), which no other host can set.
- **Personal data at the relay.** Account e-mail addresses, ids, client IP addresses and timings. The relay's
  operator is their controller and owes a notice, the retention and a hosting region; for the hosted instance
  that is GraalSystems, with EU hosting, before it opens to the public.
- **A code approved by mistake.** An owner who enters a stranger's user code binds the stranger's installation
  to their account: it gets a new id, never the owner's id or traffic. The account lists its installations and
  removes one.

## Alternatives rejected

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| A relay that terminates TLS (an HTTP reverse proxy) | it must buffer, inspect or rewrite | it sees cookies, passkey assertions, approvals and content; its operator processes everything | **Refused** |
| A wildcard certificate `*.<relay-domain>` held by the relay | one certificate for all | it is terminating TLS by another name | **Refused** |
| DNS-01 brokered by the relay as the default | a CA's ALPN validation fails through the passthrough | a DNS-write credential at the relay and a TXT API per installation, for no less trust | A fallback only |
| Extending `tranibor_api.relay` (ADR 0091) | one relay module | that is an outbound forward proxy in the API's image; this is a separate program the platform never runs | **Refused** |
| Streams multiplexed inside the control channel | connection setup dominates | a framing and flow-control protocol of ours; one slow stream holds the others | **Refused**: one dial-back per stream |
| The person's own tunnel or VPN as the only path | the person runs one already | Cloudflare Tunnel terminates TLS at Cloudflare; a VPN needs an app and an account on the phone | Documented for those who have one |
| LAN exposure (`0.0.0.0`, mDNS) | at home only | it breaks the `127.0.0.1` pin and needs certificates for a LAN | **Refused** |
| Store-and-forward of webhooks at the relay | a laptop sleeps often | it needs the plain request, so terminating TLS | **Refused**; connectors poll |
| The relay verifying vendor signatures | the installation is often asleep | the relay would hold every person's Slack, Teams and Notion secrets | **Refused** |
| The runner's door and the run's API through the relay | the installation is on a home server and the laptop elsewhere | the relay's operator would carry every run's token (ADR 0226 §12) | **Refused** |
| Ids chosen by the person | memorable addresses | squatting, look-alike names for phishing | **Refused** |

## Consequences

- A new package, a new contract and a new chart. Stories S87-42 (the server), S87-43 (the agent, the certificate
  and `choregos local relay`), S87-44 (the chart, `docs/relay.md`, the runbook), S87-47 (scenario 1 end to end
  through the relay, `tests/relay/test_scenario_one.py`) and S87-70 (webhooks) build it. This record proves
  nothing; each guarantee above names the test that will hold it.
- The relay protocol becomes a long-lived compatibility surface: dropping N-1 too early cuts old installations
  off their phones. The server's minimum moves only with a release note.
- A person who changes relay or id registers their passkeys again (ADR 0228).
- The hosted instance opens to the public only after the Public Suffix List or a limit adjustment, its terms,
  its GDPR notice and its hosting region are settled; until then it serves a pilot.

## Not proved

No relay has run. Untried: Caddy's TLS-ALPN-01 behind a passthrough on a real CA (Pebble only in CI), current
Let's Encrypt limits and Public Suffix List delays, the latency a dial-back adds on a mobile network, and iOS
Safari and Android browsers through the relay on real phones.
