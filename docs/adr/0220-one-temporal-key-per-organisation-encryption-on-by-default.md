# 0220 — One Temporal key per organisation, derived from the master key, and encryption on by default where it matters

- **Status**: accepted, 2026-11-24 (phase 2 of [ADR 0183](0183-temporal-over-tls-payloads-encrypted.md), without a KMS);
  **superseded in part** by [ADR 0225](0225-an-organisation-s-temporal-payloads-sit-under-its-own-wrapped-key-which-can-be-shredded.md),
  2026-11-25: where a key wrapper is configured, an organisation's new payloads go under a random data key
  wrapped by it, which can be rotated and shredded; §1's derived key stays for what it sealed and for a
  deployment without a key wrapper. §2 to §5 are unchanged.
- **Concerns**: `tranibor_core.temporal_connection` (`AesGcmCodec`, `organisation_scope`,
  `OrganisationDirectory`, `organisation_of_workflow`, `derive_organisation_key`,
  `TemporalSecurity.refuse_clear_in_production`), `tranibor_api.temporal.RealTemporal`, the start sites of
  the interpreter (`services/tickets.py`, `services/recurrence.py`, `routers/workitems.py`,
  `routers/findings.py`, `routers/webhooks.py`, `activities/tracker.py`), `train_client`, the API's
  lifespan and the worker's start, the chart (`global.temporal.codec`, `embedded-secrets.yaml`)

## Context

A buyer's review of ADR 0183's phase 1 wrote: "Temporal payload encryption is off by default, and one key
serves every organisation." Both are true. A deployment that never read the page ran every ticket, result
and decision in clear, in production, with nothing saying so (the worker logged `payload_codec=false`);
and one key opened the histories of every organisation, so an operator who held it for one tenant held it
for all. ADR 0183 left both to "phase 2, from a KMS", a provider chosen with the customer that needs one.
Neither gap needs a KMS to close in part.

## Decision

1. **A key per organisation, derived.** The deployment still holds a master keyring (`id:base64`, the first
   encrypts, all decrypt: ADR 0183 §3, unchanged). A payload that belongs to an organisation is sealed
   under `HKDF-SHA256(master key, info = "tranibor/temporal-payloads/org/v1:" + organisation id)`. HKDF is
   one way: an organisation's key reveals neither the master key nor another organisation's key, and a
   payload of organisation A cannot be opened with B's derived key (nor with the master key). The
   payload's metadata carries `encryption-key-id` (the master key's id, as before) and `encryption-org`
   (the organisation's id); the associated data is `<key id>|org|<org id>`, so a payload cannot be
   relabelled under another organisation. Decoding needs no context: the metadata names the key to derive.
   Rotation is the master keyring's, unchanged: every organisation's key rotates with it, and an old
   history stays readable while its master key is listed.
2. **The platform key is the master key.** A payload that belongs to no organisation (a train, a loop, a
   provisioning, a signal to a workflow the process cannot place) is sealed under the master key itself,
   with the format of ADR 0183 byte for byte (no `encryption-org`, key id as associated data). Every
   history written under ADR 0183 therefore decodes unchanged, and so does a clear payload.
3. **Where the organisation comes from.** Encoding must know it; decoding must not.
   - The caller says it: `with organisation_scope(project.org_id):` around a start. The interpreter's input
     names a project and no organisation, and `workflows/` is not touched (an extra input key would break
     `InterpreterInput(**payload)` and every history in flight), so the API's five interpreter starts and
     the catch-up's two (`activities/tracker.py`) open the scope. `tests/architecture/test_interpreter_starts_are_organisation_scoped.py`
     refuses a start outside one. A start payload that carries `org_id` is scoped without the block.
   - The codec is a `WithSerializationContext`: bound to a workflow id, it **learns** the organisation of
     that workflow from every payload it decodes and uses it for what it encodes next (the activities'
     results and the workflow's replies are encoded where its input was decoded; after a restart a replay
     decodes the history first). `OrganisationDirectory` is a bounded in-process map.
   - A process that did not start the workflow (the API signalling or querying it after a restart) reads
     the workflow's **memo** (`organisation`), written at start, once per workflow id and remembered;
     a workflow with no memo is the platform's, and that answer is remembered too.
4. **On by default where it matters.**
   - The chart: `global.temporal.codec.enabled` is `true`. With no `secretName`, the chart draws a master
     key into the Secret `<release>-temporal-codec` (`v1:<base64 of 32 random bytes>`), re-reads it with
     `lookup` on every upgrade and keeps it on uninstall (`helm.sh/resource-policy: keep`), like the other
     generated secrets. A renderer that cannot `lookup` must name a Secret.
   - The processes: with `TRANIBOR_ENV` `staging` or `prod`, `connect_temporal` (so every client, the
     workers' and the activities'), the API's lifespan and the worker's start refuse a process with no
     keyring, naming the setting, **unless** `TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS=true`
     (`global.temporal.codec.allowClear`). The chart refuses at render a staging or prod release with
     `codec.enabled: false` and no `allowClear`.
   - `dev`, `test` and the fakes need no key: nothing connects to Temporal under the fakes, and a
     development process with no keyring keeps the SDK's default converter.
5. **Nothing logs a key.** The keyring is read once into the codec; failure messages name a key id or
   an organisation id, never key material (`test_no_key_appears_in_a_failure_message`).

## Not covered

- **Not a KMS.** (Closed in part by ADR 0225: wrapped random data keys, shreddable per organisation.) The
  master key is a Secret the operator holds; whoever holds it can derive every organisation's key. This isolates organisations from each other's stored histories and from a
  leak of one derived key, not from the operator of the deployment. A per-organisation key from a KMS, wrapped
  and shredded per organisation, is still the path of ADR 0183 §phase 2, and `derive_organisation_key`
  is its seam.
- **Workflows that name no organisation are sealed under the master key** (ADR 0225 scopes every start that
  knows its organisation; a child started from workflow code still cannot): provisioning, actions, trials,
  hosted builds, session pods, reconciliation and memory loops, trains. Their payloads are ids and
  summaries; the ticket-carrying one, the interpreter, is scoped. Scoping another is a start site and an
  architecture-test entry.
- **The organisation's id is not secret.** It is in the `encryption-org` metadata of every payload, which
  Temporal reads, and in the workflow's memo. Ids, workflow ids, queue and type names stay in clear, as
  ADR 0183 says.
- **A signal to a workflow started before this ADR** carries no memo and goes under the master key; the
  workflow's own later payloads follow what it decoded last, and a master-key payload teaches no organisation.
  Histories begun before stay readable and are not re-encrypted.
- **Turning encryption off again** is as unsafe as in ADR 0183 while histories written with it are open.
- No codec server for the Temporal UI, no TLS handshake proven against a real frontend: unchanged.

## Consequences

- An upgrade from ADR 0183 needs nothing: its payloads decode, new payloads of organisations are sealed
  under derived keys. A rolled-back worker (ADR 0183's codec) cannot read an organisation's payloads
  written after the upgrade: roll forward, or keep the new workers until those workflows end.
- An installation from the chart gets encryption and a Secret it must now back up: **losing the key loses
  the histories**. `docs/deployment.md` says where the key is and how to export it.
- A production deployment that deliberately runs in clear says so in its values, and the choice is visible
  in review.
