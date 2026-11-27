# Relay operations

For whoever operates a relay ([`docs/relay.md`](../relay.md),
[ADR 0230](../adr/0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md)): abuse
reports and suspension, rotating keys and certificates, certificate limits and the Public Suffix List,
incidents, and the personal data the relay holds.

**What you can know, and what you cannot.** The relay never terminates an installation's TLS. You can
read its metadata lines (an id, when, from which address, how many bytes, why it closed) and its start
line. You cannot read what a phone sent or what an installation served: no procedure below reads
content, because the relay has none to read. Evidence about content comes from whoever reports it.

The commands assume the chart's defaults: release `relay` in the namespace `relay`, the domain
`relay.example.com`, so the Deployment `relay-choregos-relay` and the lines in
`/var/lib/choregos-relay/metadata.jsonl`. `jq` runs on your machine; the image has none.

```bash
lines() { kubectl -n relay exec deployment/relay-choregos-relay -- cat /var/lib/choregos-relay/metadata.jsonl; }
```

## Abuse reports and suspension

**How to know it is this.** Someone reports a page served at `https://<id>.relay.example.com`
(phishing, malware, a copyright claim), or a provider (Safe Browsing, a registrar) flags the domain.
A name outside your domain is not served by your relay.

1. **Take the id** from the reported name: its first label, 12 characters of `a-z` and `2-7`.
2. **Read what the relay recorded** for it. It tells you that the id was reached, when, from where and
   how much; not what was served.

   ```bash
   lines | jq -c 'select(.install_id == "abcdefghijkl")' | tail -n 20
   lines | jq -r 'select(.install_id == "abcdefghijkl") | .close_reason' | sort | uniq -c
   ```

3. **Cut the id off now, by DNS.** A record on the exact name takes it out of the wildcard
   `*.relay.example.com` (RFC 4592): add a TXT record `abcdefghijkl.relay.example.com` ("suspended",
   with the date), and the name no longer resolves to the relay. Phones and the installation's CA then
   get no address. Resolvers that cached the wildcard answer keep it for its TTL: keep the wildcard's
   TTL short (300 seconds). A client that connects to the relay's address with that server name by
   hand still reaches the installation.
4. **Suspend the id at the relay** once you can. A suspended id has its live control session closed,
   its hellos refused and its phones closed with `suspended` on their lines (`Relay.suspend`, held by
   `packages/sni-relay/tests/test_passthrough.py`). In this release that is a call in the relay's
   process: there is no operator interface yet, and restarting the relay forgets **every** pairing
   (the registry is in memory). Keep the DNS cut-off until the interface exists; restart only as the
   last resort for abuse across many ids.
5. **Tell the account holder** the id is bound to, with the report and how to answer it. In this
   release the binding of an id to its account is in the relay's process only.
6. **Record the decision** where your organisation keeps its incident records (GraalSystems: its
   operations repository): the id, the report, who decided, when, why, and when it was lifted. The
   relay keeps no such record.
7. **Lift it** by removing the DNS record (and resuming the id at the relay, once possible). If the
   domain itself was blocklisted, ask the provider for a review once the id is cut off; without a
   Public Suffix List entry, one id's page can get the whole domain blocklisted (below).

**How to check it is done.** `dig +short abcdefghijkl.relay.example.com` returns no address, and new
lines for the id stop.

**A flood from client addresses** (not an installation) shows as lines closed with
`limit_client_connections_per_minute`: the relay closes such a connection before reading anything.
For a flood beyond that, use your provider's network protections in front of the load balancer
(`passthrough.loadBalancerSourceRanges` admits ranges; it does not deny one).

## Rotating keys and certificates

Four kinds of key touch a relay; it holds two of them.

| Key | Where | Rotation |
|:--|:--|:--|
| The installation's Ed25519 key (its agent's) | the installation | the owner pairs again: a new key gets a new id (protocol 1 has no re-key message); the old id is removed from the registry, and cut off by DNS meanwhile |
| The installation's certificate for `<id>.relay.example.com`, its key, its ACME account key | the installation's edge | renewed there by the edge, through the relay like any connection; the relay never holds them |
| The relay's own host certificate and its key | the Ingress controller (`own.ingress.tls.secretName`), or the relay (`own.tls.secretName`) | below |
| The relay's Secret (`existingSecret`: `database-url`, `signing-key`) | the release's namespace | below; this release of the relay reads neither key |

**The own host's certificate.** Check when it expires:

```bash
openssl s_client -connect relay.example.com:443 -servername relay.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -enddate
```

Behind an Ingress, the controller reads the renewed Secret (cert-manager renews it) and the relay is
not touched. With `own.tls.secretName`, the relay reads the certificate when it starts: a renewed
Secret takes effect at the next restart, which in this release forgets every pairing. Prefer the
Ingress until the registry persists, or schedule the restart and tell the installations' owners.

**The relay's Secret.** Replace its data in place, and restart the relay when a release reads it:

```bash
openssl genpkey -algorithm ed25519 -out signing-key.pem
kubectl -n relay create secret generic relay-own --dry-run=client -o yaml \
  --from-literal=database-url='postgresql://relay@db.example.com/relay' \
  --from-file=signing-key=signing-key.pem | kubectl apply -f -
shred -u signing-key.pem
```

## Certificate limits and the Public Suffix List

Every installation's edge obtains its own certificate for `<id>.relay.example.com`, from its CA (Let's
Encrypt by default), and every id shares one registered domain (for `relay.example.com`,
`example.com`). Two consequences:

- **Certificate limits.** Let's Encrypt limits the new certificates issued per registered domain per
  week, for all ids together. Past it, new installations cannot obtain a certificate until the window
  moves; renewals are counted differently. Read the current figures at Let's Encrypt's rate-limits page
  before opening a relay to the public: they are not verified here (ADR 0230).
- **One domain for everybody.** Without an entry on the Public Suffix List, a browser treats every id
  as the same site for some purposes, another id's page can set cookies for the whole domain (the
  installation's session cookie is `__Host-` and is not exposed to that), and a blocklist that flags one
  id's page flags them all.

**How to know you are near the limit.** The relay does not see it: an installation's edge reports the
ACME server's `rateLimited` error. Count the certificates issued under your domain in Certificate
Transparency (crt.sh, `%.relay.example.com`) week by week.

**What to do.** Either put the relay's domain on the Public Suffix List (a pull request to the list's
repository, proven by a `_psl` TXT record; merging takes weeks to months and browsers ship it later
still; read the list's guidelines first), which makes each `<id>.relay.example.com` a registered
domain of its own; or ask
Let's Encrypt for a rate-limit adjustment for the domain. One of the two is a prerequisite of opening a
relay to the public (ADR 0230, "Consequences").

The same Certificate Transparency search is, until a story automates it, how to spot a certificate on
an `<id>` name whose key is not the installation's (see the incidents below).

## Incidents

**Phones reach no installation.** How to know: lines show `no_control_session` for every id, or no
line is written at all.

1. `kubectl -n relay get pods,svc` and `kubectl -n relay logs deployment/relay-choregos-relay`: the
   start line (`serving <id>.relay.example.com …`) or the refusal that stopped it.
2. The passthrough Service has an external address, and `*.relay.example.com` resolves to it.
3. The own host answers (`curl -sI https://relay.example.com/relay/v1/nothing` returns 404 from the
   relay): agents need it to keep their control channel. Behind an Ingress, check the controller's
   timeouts on long-lived connections.
4. A restarted relay has forgotten every pairing in this release: installations must pair again.

How to check it is fixed: lines with `client_closed` or `installation_closed` come back.

**One installation is unreachable.** Read its lines' `close_reason`: `no_control_session`, the
installation is asleep, offline or disabled the relay (nothing to do at the relay; its owner runs
`choregos local doctor`); `dial_back_timeout`, its agent cannot dial back to the own host;
`suspended`; a `limit_*`, a limit of the installation was passed (the values are `relay.limits`).

**The metadata volume is full.** The relay keeps serving and loses lines: its stderr says
`error caught by the event loop: OSError`. `kubectl -n relay exec deployment/relay-choregos-relay -- df -h
/var/lib/choregos-relay`; enlarge the claim (`persistence.size`), or shorten the retention, and say so
in your terms.

**A certificate on an `<id>` name that the installation did not ask for.** How to know: Certificate
Transparency shows a certificate for `<id>.relay.example.com` whose key is not the edge's, or issued
when the installation requested none. Whoever routes a name can obtain a certificate for it: treat it
as an interception through the relay's routing or your DNS. Tell the installation's owner, cut the id
off by DNS, ask the CA to revoke the certificate as mis-issued, and review who can change the relay,
its image and the domain's DNS.

**The relay itself is compromised** (its host, its image, its cluster credentials). What an attacker
gets: the metadata lines, the registry (accounts, ids, agents' public keys), and the routing, with
which it could obtain certificates for ids and stand in the middle. Take the relay down (installations
keep working on their own machine), rebuild it from an image you verified, rotate the own host's
certificate and the relay's Secret, watch Certificate Transparency for every id's name, and tell the
account holders. Personal data was at risk: notify your supervisory authority within 72 hours (GDPR
Art. 33) and the people concerned where the risk is high (Art. 34).

## Personal data (GDPR)

- **Controller.** The relay's operator. For GraalSystems' hosted relay, GraalSystems; for a relay you
  run, you.
- **What the relay holds.** Client IP addresses with the times, sizes and durations of their
  connections (the metadata lines); ids, the account each is bound to and its agent's public key (the
  registry; an id bound to an account is pseudonymous personal data); account e-mail addresses once the
  account side exists. Never the content of a session, which never reaches it.
- **Retention.** The metadata lines: the fixed retention the operator sets (`relay.retentionDays`),
  enforced by the relay, which deletes older lines when it starts and every hour; a backup of the
  volume must not keep them longer. The registry: until the account removes the installation (in this
  release, until the relay restarts). In memory, the times of each address's connections over the
  last minute, and the bytes each id moved this month. State all of it in the notice and the terms.
- **Hosting.** GraalSystems' hosted relay runs in the EU, a prerequisite of its public opening. A
  self-hosted relay is where its operator puts it.
- **Processors.** Your hosting provider and load balancer (they carry the same encrypted bytes), your
  DNS provider (it sees queries for the ids' names).
- **Requests.** Access: the lines of the person's ids and their registry entries. Erasure: remove the
  installation; its lines expire at the retention, which the notice states. Lines are not edited while
  the relay writes them.

## GraalSystems' hosted relay

Its deployment (values, DNS, account service, terms, notice, incident records) lives in an operations
repository that is **neither the core nor the enterprise edition**. It runs this chart and this image;
this runbook is the procedure it follows, and anything specific to that deployment belongs to that
repository, never here.
