# 0221 — The audit log forgets a verified prefix behind a checkpoint written first, under a retention rule that is off by default

- **Status**: proposed, 2026-11-24 (S80-05c, audit retention; not yet reviewed by the product owner: a migration and a
  change to the append-only trigger of [ADR 0182](0182-the-audit-log-is-a-hash-chain-the-database-keeps.md))
- **Concerns**: `tranibor_api.audit_retention` (`Pruner`, `retention_loop`), `audit_chain.ChainWalker` (a walk that
  starts at a checkpoint), the table `audit_checkpoints` (migration `e8c0a2d4f6b1`: kind `prune`, columns `pruned`
  and `retention_days`), the trigger function `choregos_audit_append_only()`, `choregos-admin audit prune`, the
  settings `TRANIBOR_AUDIT_RETENTION_*`, three metrics; takes ADR 0182 point 9 ("retention") and the retention
  item that [ADR 0207](0207-the-audit-chains-are-shipped-to-a-siem-from-a-durable-cursor.md) left open and asked to
  keep cursors meaningful

## Context

The log is a tamper-evident hash chain per organisation plus the platform's (ADR 0182), append-only by trigger,
shipped to a SIEM from a durable cursor (ADR 0207). Nothing in it ever leaves: the buyer's review of 2026-11-24 read
"the audit log keeps no retention rule". A log that cannot forget cannot meet a data-minimisation duty or a
storage budget, and a log that forgets by `DELETE` is no longer a chain (the verifier reports entries "deleted
after they were written", and rightly). Read from the code on 2026-11-24:

- The trigger refuses every `DELETE` but an organisation's own cascade; the verifier starts at entry 1 with
  `GENESIS` and needs every place up to the head.
- The chain's head row is locked by every writer until its transaction ends; a long transaction on it stalls the
  chain's writers.
- The export's cursor (`audit_export_cursors`) says what the sink has been given, per chain.
- The platform's periodic jobs are loops of every API replica, made safe by a row lock (`SKIP LOCKED`), not by an
  election (ADR 0207 point 1). The signing key of the checkpoints is kept out of the API's pods on purpose (ADR 0182
  point 7), so the API cannot sign a checkpoint.

## Decision

1. **Off by default; when on, at least 30 days.** `TRANIBOR_AUDIT_RETENTION_DAYS=0` keeps the log for ever and is
   the default, so an upgrade deletes nothing. A conservative non-zero default was discarded: any figure would
   be a compliance choice (one year for a SOC 2 audit, five or more for financial regulation) made for the
   operator, and a deletion cannot be undone. A value between 1 and 29, or a negative one, stops the API at
   start: a typo (`1` for `365`) must not empty the log. The age is the entry's `ts`.
2. **An organisation may keep longer, never shorter, and cannot start a rule.** `TRANIBOR_AUDIT_RETENTION_ORGS`
   (a JSON object, slug to days, set by the operator like the export's filter) lengthens the rule for those
   organisations. A value under the deployment's, or any value while the deployment's rule is off, stops the API at
   start. The platform's chain follows the deployment's rule. There is deliberately no organisation-side switch
   and no API path: an organisation's administrator who could shorten its own retention could erase their own
   trail, and a new path is a contract change (ADR 0001). A tenant-facing setting is a later decision.
3. **What goes: the longest prefix of a chain that is old enough, verified, and shipped.** Per chain, starting after
   the chain's latest `prune` checkpoint, the pruner reads the next entries in the order of `seq` and keeps
   the entries while each (a) is older than the rule (the walk stops at the first entry that is not, so an
   entry with a skewed `ts` never opens a gap), (b) passes `ChainWalker` (a prefix that does not verify is not
   removed: removing it would bury the break, and the entries before the break go while it and what follows
   stay, with `audit.retention.chain_broken` logged and the loop backing off), and (c) when a SIEM export covers
   the chain, has a `seq` at or under the sink's cursor. Without a cursor row nothing goes. A chain the export
   does not cover (an organisation outside `TRANIBOR_AUDIT_EXPORT_ORGS`, the platform with
   `..._PLATFORM=false`) is held by the rule alone. An export filter naming an unknown slug holds every chain.
   Consequence, said plainly: **a sink that has stopped holds retention back, and the log grows**
   (`audit.retention.held_back`, `choregos_audit_retention_held_back_total`, next to the export's lag alert).
   Deleting or renaming a cursor re-ships a chain from entry 1 into a pruned log: the export logs the discontinuity
   (ADR 0207 point 4) and ships what remains with its `prev_hash`.
4. **The checkpoint is written first, and the database enforces the order.** In one transaction the pruner
   locks the chain's head row (`FOR UPDATE SKIP LOCKED`), writes an `audit_checkpoints` row of kind `prune` (`seq`
   and `hash` of the last removed entry, `pruned`, `retention_days`, `created_at`), deletes the prefix, and records
   it in the chain as an `audit.retention.prune` entry whose payload repeats `through_seq`, `through_hash`,
   `pruned`, `retention_days`. The trigger `choregos_audit_append_only()` now lets a `DELETE` of an `audit_log`
   entry through only when a `prune` checkpoint of the same chain at or after its `seq` is already visible to the
   transaction. `UPDATE` stays refused, `audit_checkpoints` stays append-only, and row-level security keeps a
   session bounded to an organisation from writing a checkpoint. The checkpoint is **unsigned**: the key is kept
   out of the API on purpose (ADR 0182 point 7). What stands in for the signature is that the checkpoint is
   chained (the `audit.retention.prune` entry is an ordinary entry, hashed into the chain, shipped to the SIEM,
   and its `prev_hash` is the old end) and that a signed checkpoint or an anchor kept outside still vouches for
   entries after the prune point.
5. **The verifier starts at the checkpoint.** The walk of a chain with a `prune` checkpoint starts at the latest
   one: the first entry left must be the next place and link to the checkpoint's `hash`. At the end the
   checkpoint must be recorded by an `audit.retention.prune` entry of the chain with the same `through_seq` and
   `through_hash`; a checkpoint added by hand, or changed, fails there. A signed checkpoint or an anchor at the
   prune point must agree with it; earlier ones vouch for entries nobody can read and are set aside, which
   `verify` says (`entries 1 to N were removed by retention ... no longer evidence`). A prefix removed with no
   checkpoint is still "entries missing".
6. **Several replicas: the head row's lock; idempotence by construction.** The loop runs in every replica
   (`TRANIBOR_AUDIT_RETENTION_INTERVAL_S`, hourly). A replica that finds a chain's head locked skips it; one that
   starts after another committed begins after the new checkpoint and finds nothing due, so nothing is deleted or
   checkpointed twice. A delete that matches a different number of rows than the verified prefix (a fork, a
   writer outside the line) raises and rolls the checkpoint back. The head row is the lock rather than an
   advisory lock, for ADR 0207's reason: the lock is the election, per chain.
7. **In batches, because the head is held.** A transaction removes at most `TRANIBOR_AUDIT_RETENTION_BATCH` entries
   (5000) and each batch has its own checkpoint and record, so the first catch-up on a large log leaves one
   checkpoint per batch rather than a stall of the chain's writers for as long as the delete takes. The pass
   repeats until a short batch.
8. **A command and what the operator sees.** `choregos-admin audit prune [--org SLUG]… [--dry-run]` applies the rule
   once, as the loop does (exit 1 when entries due to go do not verify). `/metrics`:
   `choregos_audit_retention_pruned_entries_total`, `..._held_back_total`, `..._failures_total` (per process); logs
   `audit.retention.started|pruned|held_back|chain_broken|failed|pass_failed`. PostgreSQL only: SQLite keeps no
   chain, and the API says so and does not start the loop.

## Consequences

- With the rule on, an entry older than the rule is gone from the database once the SIEM (if any) has it. It is
  no longer evidence there; the SIEM's copy and its own retention are the operator's.
- The append-only guarantee loses its one absolute: there is now a second `DELETE` path. It is narrower than a
  flag (it needs a checkpoint row, which only the platform's scope writes), but the limit ADR 0182 names stands:
  the API's role owns the tables, and with DDL, or by writing a checkpoint and deleting behind it, it can remove
  entries. What it cannot do is leave a chain that verifies: the checkpoint must be recorded inside the chain,
  and the record is an entry the SIEM receives.
- The downgrade of the migration does not restore pruned entries and keeps the `prune` rows; an older verifier
  then reports the removed prefix as deleted. Downgrade only a deployment that never pruned.
- `audit_checkpoints` grows by one row per batch per chain per pass that prunes.

## Not decided here

An organisation's own retention setting and its console; signing the prune checkpoint (needs a signer outside the API,
with checkpoints on a schedule); shipping the checkpoint itself to the SIEM other than as its record entry; archiving
the pruned prefix to cold storage instead of dropping it; `GET /audit` showing `seq` and `hash`; a rule by action
or by entry type; the chart's `global.auditRetention` (the settings are set with `global.extraEnv` today).

## Alternatives discarded

- **A sealed archive of the prefix.** A second store to secure for every deployment; the SIEM is the archive.
- **A GUC that switches the trigger off for the pruner.** Any session can set it; a checkpoint row is a record in the
  chain's own history and a condition the platform's scope alone can meet.
- **One checkpoint per pass in one big transaction.** Holds the chain's head for the whole delete.
- **Pruning by `ts` with a `DELETE ... WHERE ts < cutoff`.** Can leave a hole (clock skew), and there would be no
  prefix a checkpoint could name.
