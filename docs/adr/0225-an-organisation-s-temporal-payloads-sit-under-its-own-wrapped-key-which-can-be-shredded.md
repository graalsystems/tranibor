# 0225 — An organisation's Temporal payloads sit under its own wrapped key, which can be shredded

- **Status**: proposed, 2026-11-25 (supersedes [ADR 0220](0220-one-temporal-key-per-organisation-encryption-on-by-default.md)
  §1 and its first "Not covered" item in part; builds on [ADR 0103](0103-secret-keks-sit-behind-a-key-wrapper.md))
- **Concerns**: `tranibor_core.temporal_connection` (`DataKeySource`, `DataKeyCache`, `install_data_keys`,
  `AesGcmCodec`, `ShreddedKeyError`), `tranibor_api.services.temporal_keys`, the table `temporal_data_keys`
  (migration `31d9f2433719`), `routers/org_security.py` (`/orgs/{org}/temporal-keys…`), `routers/admin.py`
  (`/platform/secrets/rewrap`), the API's lifespan and the worker's start, `choregos admin temporal-keys`,
  every workflow start site of the API, the orchestrator and the `software-delivery` plugin

## Context

ADR 0220 gave each organisation a Temporal key **derived** from one master key by HKDF. It named its own
limits: whoever holds the master key derives every organisation's key, so an organisation's histories cannot
be destroyed short of destroying everyone's (no crypto-shredding); only the interpreter's starts named an
organisation, every other workflow stayed under the master key; and losing the key loses the histories.

The platform already has a seam for exactly the missing piece: connector secrets are sealed with random data
keys wrapped by a `KeyWrapper` (ADR 0103), the `TRANIBOR_SECRETS_KEKS` keyring by default, HashiCorp Vault's
transit engine through the `vault-transit` extension (`plugins/vault-transit`). Nothing tied the Temporal codec to it.

## Decision

1. **A random data key per organisation, wrapped.** The first time a payload of an organisation is sealed, a
   random 32-byte key is drawn, wrapped by the deployment's `KeyWrapper` (`active_key_wrapper()`: the
   extension's, else the keyring) with the additional data `temporal-data-key|<org id>|<key id>`, and stored
   in `temporal_data_keys` (`org_id`, `key_id` = `dk-` + 16 hex digits, `state`, `wrapped_key`,
   `key_nonce`, `kek_id`, who and when). The table is under forced row-level security on the organisation,
   like `org_secrets`; the organisation's cascade deletes its rows.
2. **The codec asks, a cache keeps.** Core ships the protocol, not the store: `DataKeySource`
   (`active_data_key(org)`, `data_key(org, key id)`), implemented by the API's `DatabaseDataKeys`. Each process
   installs one (`install_data_keys`) before it connects: the API's lifespan and the worker's start do, when a
   key wrapper is configured. `DataKeyCache` holds unwrapped keys for `TRANIBOR_TEMPORAL_DATA_KEY_TTL_S`
   (300 s by default, 0 to 86 400), shared by every client of the process; a refusal is never cached. The
   wrapper's protocol is synchronous, so a wrap or unwrap runs in a thread and a Vault round trip does not hold
   the worker's event loop.
3. **The payload names its key.** A payload sealed under a data key carries `encryption-org`,
   `encryption-key-id` (the data key's id) and `encryption-key-source: org-data-key`; the associated data is
   `<key id>|org-data-key|<org id>`, so a payload cannot be relabelled to another organisation, another key or
   another kind of key. Decoding reads the metadata and needs no context, as before.
4. **Nothing older is re-encrypted.** A payload without `encryption-key-source` is decoded as ADR 0220 and
   ADR 0183 said: the derived key when it names an organisation, the master key otherwise, clear when it is
   clear. The master keyring therefore stays, and still seals what belongs to no organisation. A process
   without a key source (no key wrapper configured) seals an organisation's payloads under the ADR 0220
   derived key, as it did, logs `temporal.data_keys.derived` at start, and refuses to decode a data-key
   payload with a message that names the organisation and the key id, never key material.
5. **Rotation.** `POST /orgs/{org}/temporal-keys/rotation` (`choregos admin temporal-keys rotate`) retires
   the active key and makes a new one; retired keys still open what they sealed. The process that rotates
   drops its cached key at once; others within the TTL. A rotation of the **wrapping** key is the existing
   `POST /platform/secrets/rewrap` (`choregos admin secrets rewrap`): it now also re-wraps every live data key
   under the active KEK or transit version; the data keys, so the payloads, do not change.
6. **Crypto-shredding.** `POST /orgs/{org}/temporal-keys/shredding` (`choregos admin temporal-keys shred`),
   with `confirm` repeating the organisation's slug and a `reason`, sets every wrapped key of the organisation
   to nothing and its state to `shredded` (a tombstone keeps the id, the time and who). From then on no one,
   the key wrapper's holder included, can open what those keys sealed; the codec answers
   `ShreddedKeyError`. Audited (`org.temporal_keys.shred`, with the reason and the key ids). The answer lists
   what is not erased (below). The organisation's next payload gets a new key. Who: the organisation's
   `org_admin` (on a single-organisation instance, the platform's administrator is that person); the
   organisation owns the data, and the gesture touches no other organisation.
7. **The other workflows name their organisation.** Every start of the API, the orchestrator and the
   `software-delivery` plugin that knows its organisation opens `organisation_scope(org_id)`: actions (the
   API's three starts, the relay, the activity's), trials, provisioning, run cancellations, hosted builds,
   session pods, knowledge syncs (API and worker) and distillations, the catch-up and memory loops (the API's
   activation, the provisioning activity, the worker's start, the memory signal's start), findings triage
   (signal-with-start), the release train (signal-with-start), and the child interpreters `children.py`
   starts from an activity (which ADR 0220's guard did not see). Starts from an activity now carry the memo,
   and `signal_workflow` reads it, as the API's gateway does. `tests/architecture/test_workflow_starts_are_organisation_scoped.py`
   refuses a new unscoped start, with an allowlist that must give its reason.

## Not covered

- **What shredding does not erase** (said by the API's answer, the CLI and `docs/security.md`): Temporal's own
  metadata (workflow ids and types, task queues, timestamps, status); search attributes and the memo (the
  organisation id is there and in every payload's metadata); payloads sealed before this ADR (derived key)
  or under the master key; **database backups** taken before the shredding, which still hold the wrapped key
  (whoever restores one and holds the KEK or the transit key reads those payloads again: the shredding is
  complete once those backups have expired); a key another process still caches, for the TTL at most.
- **Workflows that cannot name an organisation**: a child workflow started from **workflow code**
  (`FindingsTriage` starting interpreters): the SDK encodes its input with the child's id as the only
  context, and workflow code cannot open a scope the codec sees. Those children start under the master key,
  and their own payloads follow what they decode. Making them an activity's start is a workflow change (a
  `patched` and a replayed history), left to its own story. A workflow started before this ADR keeps the
  key its history carries.
- **Open workflows of a shredded organisation** fail their next task (their history cannot be decoded). The
  route does not terminate them; terminate them first.
- **Not a per-organisation KEK.** The wrapping key (keyring or transit key) is the deployment's, shared by
  organisations; crypto-shredding destroys the organisation's wrapped data keys, not a key in Vault. A transit
  key per organisation is possible with the same seam and is not built.
- **Losing the keys still loses the histories**: now the database rows *and* the wrapping key are needed;
  `docs/deployment.md` says what to back up.
- No codec server for the Temporal UI; no rolling upgrade proven on a real cluster.

## Consequences

- An upgrade needs nothing when `TRANIBOR_SECRETS_KEKS` (or Vault) is configured: new payloads of an
  organisation go under data keys, old ones decode. A rolled-back worker (ADR 0220's codec) cannot read a
  data-key payload: roll forward. A deployment without a key wrapper keeps ADR 0220's behaviour and the
  console's list says the keys cannot be shredded.
- The migration adds a table; it needs a human review, as every security path here does.
- Every process that decodes an organisation's payload may read the database once per key per TTL; with
  Vault, that is one transit `decrypt` too.
