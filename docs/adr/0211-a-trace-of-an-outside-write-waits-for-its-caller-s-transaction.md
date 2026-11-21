# 0211 — A trace of an outside write waits for its caller's transaction

- **Status**: proposed, 2026-11-21 (story S84-05, finding #2031)
- **Concerns**: `apps/api/src/tranibor_api/db/session.py` (`after_end`, `wrote_in_transaction`,
  `run_after_end`), `apps/api/src/tranibor_api/services/plugin_tracker.py` (the audit hook), the owners
  of a session (`session_scope`, `deps.get_db`, `deps.get_platform_db`, `outposts/rendezvous.in_scope`)

## Context

A plugin tracker's write (ADR 0096, "The tracker half") happens in the server's system, so its audit
entry `tracker.plugin.<tool>` is written in a transaction of its own: the trace must not roll back with
the caller's. The hook wrote it at once, from a second connection, in the middle of the caller's
transaction. When that transaction had already written — `mirror_state` updates the run before it sets
the tracker's state, `create_human_request` flushes its row before it comments, `close_out` records the
outcome before it closes — the second writer waited for the lock its own caller held. On SQLite (the
default database of a local install) it waited 5 s, failed with `database is locked`, and the failure
was logged and swallowed: the write happened, its trace did not (#2031). On PostgreSQL nothing was
lost, but every such write held a second pooled connection while the first waited for it.

## Decision

1. **A session knows whether its transaction wrote.** `wrote_in_transaction(session)`: an executed
   INSERT, UPDATE or DELETE, or a flush, since the transaction began; a commit or a rollback clears it.
   A pending change that was not flushed takes no lock and does not count.
2. **`after_end(session, callback)`** runs `callback` once the transaction has ended, committed OR
   rolled back — the counterpart of `after_commit` (ADR 0085), which runs only after a commit. Every
   owner of a session drains it: `run_after_commit` after a commit, `run_after_end` after a rollback,
   before the error is raised again. Best effort, like `after_commit`: a crash between the end and the
   callback loses it.
3. **The audit hook defers only when it must.** When the caller's transaction has written, the entry
   is written once that transaction ends, still in a transaction of its own, so it survives the
   caller's rollback. Otherwise it is written at once, as before.

## Consequences

- A plugin tracker write made after its caller wrote is audited on SQLite as on PostgreSQL, the call no
  longer waits 5 s, and no second connection is held during the caller's transaction.
- The entry is written after the caller's transaction instead of during it: its `ts` is the moment
  the caller ended, a few milliseconds later. The chain (ADR 0182) is unchanged: one row, written alone.
- A new owner of a session must drain `run_after_end` after its rollback, as the four owners do; a
  session nobody drains loses what waited on it. `apps/api/tests/test_after_end.py` holds the owners to
  it.
- Not done: an outbox table relayed like `action_relay`, which would survive a crash between the end of
  the transaction and the write. The trace of a tracker write does not start anything; best effort is
  what `after_commit` already accepts.
