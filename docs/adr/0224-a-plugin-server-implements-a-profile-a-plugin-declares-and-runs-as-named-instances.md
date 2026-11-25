# 0224 — A plugin server implements a profile a plugin declares, and runs as named instances

- **Status**: proposed, 2026-11-25
- **Concerns**: `packages/contracts/schemas/plugin.schema.json` (`contents.profiles`),
  `packages/contracts/schemas/plugin-server.schema.json` (`implements`), a new
  `packages/contracts/schemas/plugin-profile.schema.json`, the agent catalogue entry's `mcp_servers`
  (`profile`), `packages/contracts/openapi.yaml` (instance routes), `services/plugin_installer.py`,
  `services/plugin_servers.py` (`object_name`, `declaration`, `provision`, `egress_of`),
  `services/broker.py`, `db/models/connectors.py` (`org_connectors`), the console's Catalogue → Plugins
  and connector pages. Amends [ADR 0096](0096-a-plugin-s-code-runs-as-an-mcp-server-the-platform-runs.md)
  §1, §4 and §8; builds on [ADR 0034](0034-connectors-by-capability.md),
  [ADR 0058](0058-connector-secrets-are-write-only-values.md),
  [ADR 0070](0070-network-access-is-bounded-and-enforced-per-run.md),
  [ADR 0074](0074-catalogue-rights.md), [ADR 0093](0093-an-agent-ships-as-a-plugin.md) and
  [ADR 0149](0149-a-tool-may-live-at-an-outpost.md); respects
  [ADR 0181](0181-the-contracts-speak-no-trade.md) (the core names no trade). Contract changes: yes
  (four, listed in [Consequences](#consequences)), before the 1.0 freeze of
  [ADR 0168](0168-the-surfaces-frozen-for-1-0.md).

## Context

An IT department asks for agents that do its daily operations: DNS records, the LAN and Wi-Fi, the
identity provider, GitOps drift, on-call alerts, backups, certificates. The first version of eight such
plugins (graalsystems/tranibor-agents#55 to #62) has one plugin per product: `ovh-dns`, `unifi`,
`keycloak-admin`, `argocd`, and so on. Each one ships its agent, and the agent's instructions and tool
names belong to the product. Reviewing them, the project lead asked for something else:

- **Generic agents, reusable from one customer to the next.** A `dns-steward` that calls
  `list_records`, `plan_changes` and `create_record` whoever hosts the zone, OVHcloud, GoDaddy or
  Cloudflare. The implementation is decoupled. We ship the implementations we need, and another customer
  can plug in theirs.
- **Servers that show up in the console and that an administrator configures there**: tokens and
  secrets (an OVHcloud consumer key, a UniFi API key) and options (URLs, endpoints, allow-lists), typed
  in a form drawn from what the server declares.
- **A server that can be instantiated more than once.** Two DNS providers in one organisation (OVHcloud
  and GoDaddy) must both be usable, and so must two accounts at the same provider.

Checked against `main` on 2026-11-25:

1. **Visible and configurable: mostly there.** A plugin server's `config_schema` is drawn by the
   console's `SchemaForm` (`apps/web/src/components/schema-form.tsx`). Its `secret_fields` are sealed and
   write-only (ADR 0058). Each change rolls the Deployment, and a server whose schema is not yet satisfied
   stays `unconfigured` (ADR 0096 §7). The form cannot draw fields that depend on another field: a
   `oneOf` is drawn only when it is a choice between constants. `secret_fields` names top-level fields
   only. One server that holds several providers would need both.
2. **One instance per organisation and server.** `plugin_servers.object_name(org_id, server)` names the
   Deployment, the Service, the NetworkPolicy and the Secret. The organisation connector is named after
   the server (ADR 0096 §4), and `org_connectors` is unique on `(org_id, name)`
   (`uq_org_connectors_org_name`).
3. **An agent names a connector, never a kind of thing.** A catalogue entry's `mcp_servers` is
   `{connector, tools}`. A generic agent cannot say "every DNS provider this project may use".
4. **`implements` knows one profile, the core's.** `plugin-server.schema.json` limits `implements` to
   `["tracker"]`, a profile whose eight tools the core defines in `plugin-tracker-profile.schema.json`
   and checks answer by answer. Adding `dns`, `identity` or `lan` to that enum would put trades into the
   core's contracts, which ADR 0181 rules out.
5. **A plugin server cannot reach a private address.** Its NetworkPolicy lets egress through to public
   addresses only, never to the private, link-local or carrier-grade ranges (ADR 0096 §8). A UniFi
   console, an internal Keycloak, an Argo CD, a Grafana or a cluster's API server usually lives on a
   private address. Today the one way to reach such a system is a tool the outpost's operator runs
   (ADR 0149), which a plugin's server is not.

## Decision (proposed)

### 1. A profile is a contract a plugin declares, not one the core defines

A plugin may declare **profiles** (`contents.profiles: [profiles/dns.yaml]`), each validated by a new
`plugin-profile.schema.json`:

```yaml
# plugins/dns/profiles/dns.yaml
name: dns            # ^[a-z][a-z0-9-]{0,31}$, unique within the plugin
version: 1           # an integer; a breaking change is a new version, and both may be served
description: Authoritative DNS zones and their records, whatever the provider.
tools:
  - name: get_provider
    access: read
    input:  {type: object, additionalProperties: false, properties: {zone: {type: string}}}
    output: {$ref: "#/$defs/provider"}
  - name: list_records
    access: read
    input:  {...}
    output: {...}
  - name: create_record
    access: write
    human_approval: false     # a floor: an implementation may raise it, never lower it
    platform_arguments: [action_key]
    input:  {...}
    output: {...}
  # ...
$defs: {...}
```

A profile is named `<plugin>/<name>@<version>`, for example `dns/dns@1`. That name is the profile's
identity, and the core treats it as opaque: it validates a profile's shape and compares servers with it,
but it never reads what a profile means. The core keeps its own `tracker` profile as it is. A
plugin-declared profile is a new family beside it, not a replacement.

*Rejected: profiles in the core's contracts* (`implements: [dns]`). Each trade would become a contract
change of the core, and the core would name DNS, identity and Wi-Fi (ADR 0181).
*Rejected: a naming convention with no check.* An agent would call `dns__create_record` and find out at
run time that a server serves it with another meaning or with weaker governance.

### 2. A server implements a profile, and the installer proves it

`plugin-server.schema.json` lets `implements` take, besides `tracker`, profile names
`<plugin>/<name>@<version>`. The plugin that declares the profile must be among the implementing
plugin's `requires.plugins`. At validation (`tranibor plugin validate`) and again at install, a server
that implements a profile must:

- declare in `operations` every tool of the profile, with the same `access`;
- never set a governance weaker than the profile's: `human_approval` and `platform_arguments` may be
  added, never removed;
- serve an input schema that accepts every input the profile's schema accepts, and an output schema that
  the profile's accepts. The comparison is structural on the subset of JSON Schema that profiles may use,
  listed in `plugin-profile.schema.json`. A profile that uses anything outside that subset is refused at
  validation, so the comparison never has to guess.

A server may serve more tools than the profile's own. The profile's agents never see them (§4).

### 3. A plugin server runs as named instances

A plugin's server is installed once by default, as today: one instance named after the server. An
administrator may add **instances**, each with a name `^[a-z][a-z0-9-]{0,47}$` that is unique among the
organisation's connectors, for example `dns-ovh` and `dns-ovh-customer-b` of the server `dns-ovh`.

- **Each instance is its own organisation connector.** It owns its configuration and sealed secrets
  (ADR 0058), its `_server_token`, its `ConnectorOperation` rows (so policies and groups are set per
  instance), its egress hosts and its Kubernetes objects. `object_name(org_id, instance)` replaces
  `object_name(org_id, server)`. The default instance keeps the server's name, so the objects and
  connectors installed before this ADR keep their names and nothing moves.
- **Routes** (contract change): `POST /orgs/{org}/plugins/{plugin}/servers/{server}/instances` with
  `{name}` creates an `unconfigured` instance, and `DELETE …/instances/{name}` removes one. The connector
  routes keep refusing to create or delete a plugin's connector directly (ADR 0096 §4). Uninstalling the
  plugin removes every instance. Upgrading it re-applies each one.
- **An instance's agent-facing name is its connector name**, so its tools are
  `<instance>__<operation>`, as ADR 0096 §4 already says of the single instance.

*Rejected: one server holding several providers or accounts* (an array of `{provider, credentials}` in
one configuration). One pod would hold every provider's credentials and the union of their egress hosts,
one policy would govern them all, and the form would need conditional fields and nested secrets. The
recommended packaging is one plugin per provider (`dns-ovh`, `dns-godaddy`), each with a flat
configuration and its own image, all implementing the profile of a profile plugin (`dns`).

### 4. An agent may name a profile instead of a connector

A catalogue entry's `mcp_servers` accepts `{profile: "dns/dns@1", tools: [...]}` beside
`{connector, tools}` (contract change). When a run starts, the broker resolves a profile entry into every
organisation connector whose server implements that profile and that the project may use. This follows
the same rules as any connector (activation and catalogue rights, ADR 0074; the policy, the groups and the
agent's selection, ADR 0034 §6). The tools an entry lists must be tools of the profile, and only those
tools are offered, under each instance's name (`dns-ovh__list_records`, `dns-godaddy__list_records`).
Each call is governed exactly as a call to a named connector: the instance's own operations, policies,
approvals and ledger. A profile entry that resolves to no connector opens nothing; the run starts and
the agent is told so (a `profile.unbound` line in its context, not a failure).

The profile's own first tool should let an agent tell the instances apart. The recommended pattern is a
`get_provider` read that answers `{instance, provider, zones|sites|realms…}`, and a profile plugin's
agent reads it before anything else. The core does not require it.

### 5. The console shows profiles, servers and instances

Catalogue → Plugins shows, for each installed plugin, its profiles, its servers and, for each server, its
instances with their state (`unconfigured`, `starting`, `ready`, `failing`), plus an **Add instance**
button. A profile's page lists its implementations and their instances. An instance's page is the
connector page we already have: the form drawn from the server's `config_schema`, the secrets sealed, the
operations and their policies. `SchemaForm` gains two hints the servers already need: `x-help` (one line
under a field) and `x-link` (a documentation link). With one plugin per provider, it needs no conditional
fields.

### 6. Reaching a private system (to decide; two options)

- **6a. A deployment may let plugin servers reach named private ranges** (recommended first). The
  operator lists CIDRs in `plugins.servers.egress.privateCidrs`, off by default and refused on a
  multi-organisation deployment unless each CIDR is also bound to one organisation
  (`plugins.servers.egress.privateCidrsByOrg`). An instance's egress host that resolves into a listed
  range is admitted, against the organisation's network bounds (ADR 0070), when it is typed. The
  NetworkPolicy then opens exactly that host and port, and with Cilium a `toFQDNs` or `toCIDR` rule. The
  metadata and link-local ranges stay closed whatever is listed. This suits a deployment that runs
  inside its customer's network, which is the case of most self-hosted installs.
- **6b. An instance may be placed at an outpost** (a later ADR). The outpost runs the server's pinned
  and signed image beside its daemon. The platform keeps the declaration, the operations and the
  governance, and calls it through the rendezvous (ADR 0149 §3 to §5). The configuration typed in the
  console is sealed to the claiming instance's X25519 key (ADR 0147 §2), so the platform never holds it
  in clear on the outpost's side. This suits a SaaS platform and remote sites. It is larger work, and it
  needs a security review of the image's supply chain at the outpost.

## Consequences

- **Contract changes, all additive:** `contents.profiles` in `plugin.schema.json`; profile names in
  `implements` (`plugin-server.schema.json`); the new `plugin-profile.schema.json`; `mcp_servers[].profile`
  in the agent catalogue entry; the two instance routes in `openapi.yaml`. A core before this ADR refuses
  a plugin that uses any of them, so such plugins declare the first core release that has them in their
  `core` range.
- **Migration:** none for existing installs. Each existing plugin server is its default instance, under
  the same name.
- **Publishing:** one more plugin per provider, but each is small. The profile plugin carries the agents,
  skills, templates, evals and a **conformance suite** (pytest, run against any implementation's fake)
  that an implementation in another repository can run in its own CI.
- **Cost of a run:** a profile entry may resolve to several instances, and each one's tools are listed to
  the agent. The broker caps the instances it resolves for one entry (16 by default) and says which ones
  it left out.
- **First users:** the profiles `dns`, `identity`, `lan`, `gitops`, `observability`, `backup` and
  `certificates` in graalsystems/tranibor-agents, with the implementations `dns-ovh` (+ `dns-cloudflare`
  or `dns-godaddy`), `identity-keycloak`, `lan-unifi`, `gitops-argocd` (+ `gitops-flux`),
  `observability-grafana` (+ `observability-prometheus`), `backup-velero` and `certificates-cert-manager`.
  `joiner-leaver` names the `identity` profile.

## Stories

1. Contracts: `plugin-profile.schema.json`, `contents.profiles`, `implements` names,
   `mcp_servers[].profile` (contract-change PR).
2. `tranibor plugin validate`: a profile's shape and allowed subset; an implementation's conformance
   (operations, access, governance floor, schema compatibility).
3. Instances: `object_name` per instance, the routes, the installer's per-instance provision and upgrade,
   uninstall of all instances, and the migration test that existing installs keep their names.
4. Broker: profile resolution, the cap, `profile.unbound`, and the ledger lines naming the instance.
5. Console: profiles, servers and instances, Add instance, `x-help` and `x-link`.
6. Egress 6a (if accepted): `privateCidrs`, `privateCidrsByOrg`, NetworkPolicy and Cilium rules, and the
   check at typing.
7. Plugins (graalsystems/tranibor-agents): the seven profile plugins and their implementations, rebuilt
   from #55 to #62, which close unmerged.

## Alternatives discarded

- **Profiles in the core** — §1.
- **One server, several providers** — §3.
- **A convention without a check** — §1.
- **Copies of the plugin under other names** (`dns-ovh-2`) to get a second instance: it duplicates the
  catalogue, every copy needs a second approval and an upgrade, and the image is published twice for
  nothing.
