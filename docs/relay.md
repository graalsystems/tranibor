# Running a relay

A personal installation publishes one port, on `127.0.0.1` (ADR 0228). A phone away from that
machine reaches it through a **relay**: the installation's relay agent dials out to the relay, and the
relay routes the phone's TLS connection to that installation by the name the phone asked for,
without ever terminating it
([ADR 0230](adr/0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md)).

This page is for whoever runs a relay: what it sees and keeps, what it never does, how to run one,
and how an installation pairs with it. Anyone may run one. GraalSystems runs a default one, from
elsewhere (see the last section). The operator's procedures (abuse, suspension, keys, certificates,
incidents, personal data) are the runbook,
[`relay-operations.md`](runbooks/relay-operations.md).

> Where this page and the code disagree, the code wins. The relay is `packages/sni-relay`
> (`tranibor_sni_relay`), its contract is `packages/contracts/relay.yaml` (`/relay/v1`), its
> command is `choregos-relay` (`packages/sni-relay/src/tranibor_sni_relay/server.py`), its image is
> `docker/relay.Dockerfile` and its chart is `charts/choregos-relay`.

## What the relay does

The relay is one process with two listeners.

- **The SNI listener** (443 for the world; 9443 inside the image) takes phones' connections to
  `<id>.<relay-domain>`. It reads the phone's TLS ClientHello, within 16 KiB and 5 seconds, takes the
  server name from it, and routes `<id>.<relay-domain>` to the one installation that holds that id
  and has a live control session. It then splices the ClientHello and every byte after it, both
  ways, to that installation's dial-back, and parses nothing more. Anything else is closed: no
  server name, a malformed or out-of-bounds ClientHello, a name outside the domain, an unknown or
  suspended id, no control session, a limit passed.
- **The relay's own host** (`https://<relay-domain>` by default; 8443 inside the image) serves the
  installations' agents under `/relay/v1`: the pairing by device code, one signed control channel
  (a WebSocket) per installation, and the dial-backs. For each phone connection, the relay sends the
  agent a stream id, a single-use ticket and the phone's IP address, and no host and no port; the
  agent dials back to the relay's own host with the ticket and splices to its own edge.

The installation's edge holds the certificate for `<id>.<relay-domain>` and its key. It obtained
them itself, by ACME TLS-ALPN-01 through the relay (the CA's validation connection is routed like a
phone's), and they never leave it.

## What the relay sees

| It sees | Because | It keeps |
|:--|:--|:--|
| The ClientHello's server name, `<id>.<relay-domain>` | it routes on it | the id, on the connection's metadata line; never the name of a connection it did not route |
| The rest of the ClientHello (ALPN, cipher suites, extensions) | it reads the ClientHello whole within its bounds | nothing |
| The client's IP address and port | it is the connection's peer | the address, on the metadata line; in memory, the times of its connections over the last minute, to count the per-address limit; the phone's address is also sent to the installation, whose edge sees it as the client's |
| The sizes and times of encrypted records | it splices them | the bytes each way and the duration, on the metadata line |
| The content of a phone's session: pages, cookies, passkey assertions, approvals, webhooks' bodies | it never does: TLS ends at the installation's edge | nothing |
| An agent's pairing request: its Ed25519 public key | the pairing binds that key to an id | the key, the id and the account, in its registry |
| An agent's signed control hello | it verifies the signature against the paired key | the live session, in memory |

Any network on the path sees what the relay sees of a connection: the ClientHello in clear (server
name and ALPN), the addresses, the sizes and the timing of encrypted records.

## What the relay keeps

**One metadata line per connection the SNI listener accepted, and nothing else.** Each line is
validated against `packages/contracts/schemas/relay-metadata-line.schema.json` before it is written,
one JSON object per line, in `TRANIBOR_RELAY_METADATA_PATH` (`/var/lib/choregos-relay/metadata.jsonl`
in the image, on the chart's own volume):

| Field | What |
|:--|:--|
| `install_id` | the installation's id; `null` when the connection ended before one was known |
| `started_at`, `ended_at` | when the relay accepted it, when it closed it |
| `client_ip` | the phone's IP address |
| `bytes_in`, `bytes_out` | bytes from the phone towards the installation (the ClientHello included), and back |
| `duration_ms` | how long it lasted |
| `close_reason` | why it ended: `client_closed`, `installation_closed`, `idle_timeout`, `no_sni`, `malformed_client_hello`, `client_hello_too_large`, `client_hello_timeout`, `outside_domain`, `unknown_id`, `suspended`, `no_control_session`, `dial_back_timeout`, a limit (`limit_concurrent_streams`, `limit_streams_per_minute`, `limit_bytes_per_month`, `limit_client_connections_per_minute`), `relay_shutdown`, `error` |

No server name, no ALPN, no cipher list and no byte of a stream: the relay never has a header, a path
or a body to log.

**The retention is the operator's, and it is fixed.** `TRANIBOR_RELAY_RETENTION_DAYS` (the chart's
`relay.retentionDays`) is required: the relay refuses to start without it. It deletes the lines older
than the retention when it starts and every hour after. State that retention in your terms and in
your notice; for GraalSystems' hosted relay, its terms state it. Do not ship the file to a store that
keeps lines longer.

**Besides the lines**, the relay holds in memory, and in this release only in memory: its registry
(each id, the account it is bound to, the agent's public key, when it was paired, a suspension and its
reason), the pairings under way (a device code lives ten minutes and is forgotten ten minutes after),
the live control sessions, and the counters of its limits. A restart forgets all of it (see [What is not built yet](#what-is-not-built-yet)).

The process writes, on stderr, one line when it starts (what it serves, the retention), its refusal
when a configuration is refused, and the type of an error the event loop caught, never the connection
it came from. Nothing about a connection.

## What the relay never does

- **It never terminates an installation's TLS.** It holds no certificate and no private key for any
  `<id>` name: its configuration has no field for one, and the SNI listener is opened without TLS. A
  wildcard certificate `*.<relay-domain>` at the relay is refused by ADR 0230, since that would be
  terminating TLS by another name. Its own host has a certificate of its own, for its own name only.
- **It never carries a runner's traffic**, the run's API or the outposts' door. A personal runner
  calls its installation on loopback (ADR 0226). Where TLS ends, the installation answers 404 to
  `/api/v1/internal/*`, `/outposts/*` and `/auth/owner` for a request that came through the relay,
  decided by the door it came in by and never by `Host`
  (`apps/api/tests/test_owner_sign_in.py::test_the_relay_door_does_not_reach_the_runs_api_or_the_outposts_door`).
  The relay itself cannot tell one path from another: it never sees one.
- **It never names a destination.** Nothing it sends an agent holds a host or a port, and an agent
  refuses a message that does: the agent connects to its own edge only.
- **It never stores and forwards.** An installation without a control session (asleep, offline,
  relay disabled) has its connections closed, and nothing but their lines is written.
- **It carries no Web Push.** Notifications go from the installation to the push service (ADR 0194).

## Run your own relay

What you need: a domain of your own for the relay (`relay.example.com` below), a Kubernetes cluster
whose load balancers pass TCP through on 443, a container registry, and a certificate for the relay's
own host (cert-manager, or your own).

1. **Build the image.** No release publishes it yet.

   ```bash
   docker build -f docker/relay.Dockerfile -t registry.example.com/choregos-relay:0.29.5 .
   docker push registry.example.com/choregos-relay:0.29.5
   ```

   Behind a TLS-inspecting proxy, add `--build-context build-ca=<a directory holding its authority>`
   (`docs/deployment.md`, "Building behind a TLS-inspecting proxy"). The build checks that the relay
   starts and finds its contract; the image runs as user 1000 on 9443 and 8443, without any
   capability.

2. **Create the relay's Secret.** The chart names it and never creates it.

   ```bash
   openssl genpkey -algorithm ed25519 -out signing-key.pem
   kubectl create namespace relay
   kubectl -n relay create secret generic relay-own \
     --from-literal=database-url='postgresql://relay@db.example.com/relay' \
     --from-file=signing-key=signing-key.pem
   ```

   This release of the relay reads neither key (its registry is in memory and it signs nothing of its
   own); the pod mounts both, so that the release that reads them needs no new value.

3. **Write your values** and install. `charts/choregos-relay/values/example.yaml` is a complete
   example: the own host behind an nginx Ingress with a cert-manager certificate.

   ```yaml
   # my-relay.yaml
   image: { registry: registry.example.com, repository: choregos-relay, digest: "sha256:…" }
   relay: { domain: relay.example.com, retentionDays: 30 }
   existingSecret: relay-own
   own:
     ingress:
       enabled: true
       className: nginx
       annotations:
         nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
         nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
       tls: { secretName: relay-own-host-tls }
   ```

   ```bash
   helm install relay charts/choregos-relay --namespace relay --values my-relay.yaml
   ```

   The chart refuses what the relay would refuse (no domain, no retention, no Secret, no image), an
   annotation on the passthrough Service that would terminate TLS or prepend a PROXY header, and an
   own host published without TLS.

4. **Point DNS at it.** Two records: `*.relay.example.com` at the passthrough Service's address
   (`kubectl -n relay get service relay-choregos-relay-sni`), and `relay.example.com` at the Ingress
   (or at the own host's Service). A record for one name beats the wildcard: that is also how one id
   is cut off in an emergency (the runbook).

5. **Check it.** `kubectl -n relay logs deployment/relay-choregos-relay` shows the one line the relay
   writes when it starts. A TLS client to a name no installation holds is closed, and one line with
   `unknown_id` appears:

   ```bash
   openssl s_client -connect abcdefghijkl.relay.example.com:443 -servername abcdefghijkl.relay.example.com </dev/null
   kubectl -n relay exec deployment/relay-choregos-relay -- tail -n 1 /var/lib/choregos-relay/metadata.jsonl
   ```

**What the load balancer in front of the SNI listener must do**: pass TCP through on 443 and nothing
more. It must not terminate TLS, must not speak HTTP, and must not prepend a PROXY protocol header (the
relay reads the first bytes as a ClientHello and closes the connection). It should keep the client's
address (the chart's `externalTrafficPolicy: Local`): under `Cluster`, every line holds a node's
address and every phone counts against the same per-address limit. The relay speaks TCP only, so
nothing reaches it over HTTP/3.

**The own host** has two ways out, each with its cost. Behind an Ingress, the controller terminates
its TLS and the relay sees the controller's address for every agent: the per-address limits on hellos
and pairings count that one address (raise `relay.limits.pairingsPerMinute`), and the controller must
keep a control channel open for days. With `own.service.type: LoadBalancer` and `own.tls.secretName`,
the relay terminates its own host's TLS and sees each agent's address, but reads a renewed certificate
only when it restarts, which in this release forgets every pairing.

**Without Kubernetes**, the image runs as is:

```bash
docker run -d --read-only --cap-drop ALL --tmpfs /tmp \
  -v relay-lines:/var/lib/choregos-relay \
  -e TRANIBOR_RELAY_DOMAIN=relay.example.com -e TRANIBOR_RELAY_RETENTION_DAYS=30 \
  -p 443:9443 -p 127.0.0.1:8443:8443 registry.example.com/choregos-relay:0.29.5
```

with a reverse proxy holding the own host's certificate in front of 8443. `choregos-relay check` reads
the same variables and says what the relay would serve, without opening anything. The variables are
listed in `packages/sni-relay/src/tranibor_sni_relay/server.py`.

## How an installation pairs

1. On the installation, the owner runs `choregos local relay enable` (S87-43, not built yet). The
   agent makes its Ed25519 key and asks the relay for a device code
   (`POST /relay/v1/pairing`, signed with that key; the request carries no id, and one that names an
   id is refused).
2. The command shows a user code (`BCDF-GHJK`: eight consonants) and where to enter it, the relay's
   verification URI (`https://<own host>/pair` by default). The owner enters it in their relay
   account. A code entered by mistake binds the stranger's installation to the owner's account under a
   new id: it never gets the owner's id or traffic, and the account removes it.
3. The relay draws an id of 60 random bits, written as 12 base32 characters; nobody chooses one. It
   binds the id to the account and to the agent's public key. The agent, polling, gets the id and its
   hostname `https://<id>.<relay-domain>`.
4. The command sets the installation's public URL to that hostname and restarts what reads it. The
   agent opens its control channel, signing its hello with the paired key over the relay's nonce
   (within 60 seconds of the relay's clock); the edge obtains its certificate through the relay.
   Passkeys are bound to that hostname: a person who changes relay or id registers them again.

An installation points at a relay by its relay URL, a setting of the agent whose default names
GraalSystems' hosted relay (S87-43). A self-hosted relay is a peer of the hosted one: the installation
names it instead.

## What is not built yet

Said plainly, because a relay deployed from this chart today cannot do all of the above:

- **No account page and no approval door.** Nothing serves the verification URI; a user code is
  approved only in the relay's own process (`RelayRegistry.approve`). A deployed relay cannot complete
  a pairing until a story serves it.
- **No persistence.** The registry is in memory: a restart forgets every pairing, and every
  installation must pair again under a new id (and its owner register their passkeys again). The
  chart therefore runs one replica and stops a drain at it (`podDisruptionBudget.minAvailable: 1`).
  `TRANIBOR_RELAY_DATABASE_URL` and the signing key are mounted and not read.
- **No operator interface.** Suspending, resuming or removing an id is a call in the relay's process
  (`Relay.suspend`); the runbook says what to do meanwhile.
- **No published image or chart.** The CI builds and scans the image (`images` job); the release does
  not push it or the chart.
- **No watch of Certificate Transparency** for a certificate on an `<id>` name whose key is not the
  edge's (ADR 0230, "Recorded, and not prevented").
- **One replica.** Control sessions live in one process.

## GraalSystems' hosted relay

GraalSystems operates the default relay. Its deployment (its values, its DNS, its account service,
its terms and its notice) lives in an operations repository that is **neither this core nor the
enterprise edition**; nothing in the core depends on it, and it runs this same chart and image. It
opens to the public only once the Public Suffix List entry or a certificate-limit adjustment, its
terms, its GDPR notice and its hosting region in the EU are settled (ADR 0230, "Consequences");
until then it serves a pilot.
