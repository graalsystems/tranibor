# Single node: restore from the nightly backup

For an installation made with `charts/tranibor/values/single-node.yaml` ([ADR 0184](../adr/0184-a-supported-single-node-installation.md)).
What the backup holds: every database of the embedded PostgreSQL (`pg_dump -Fc --create`), Temporal's
SQLite file (an online copy) in `dev` mode, the memory as Ecphoria backs it up itself, and the object
store when `backup.objectStore` is on.
A `server` mode Temporal (`choregos-temporal.mode: server`) has no file: its two databases are in the
PostgreSQL dump when they are on the embedded PostgreSQL, and the backup mounts no Temporal volume (#2156).
The memory is never copied as files: the backup's `memory` step asks the running Ecphoria for its own
backup (`POST /api/v1/admin/backup` with the platform's admin key, `choregos-memory`), which exports its
stores and its vector index with a `manifest.json` of digests written last, checks the digests on its
copy, and keeps it under `memory/ecphoria/` (S80-41). Ecphoria is not stopped for it. A copy of its
DuckDB files taken while it ran did not replay, and the restored Ecphoria ran empty without an error
(#2185); the lexical index (`*.fts.sqlite`) is not carried, it is rebuilt from the restored memories.
Ecphoria answers an admin call within 30 seconds (its request timeout): a memory whose export takes
longer fails the backup with a 504, loudly; that size has not been measured.
What it does not hold: the site's secrets (`choregos-api-secrets`, `choregos-oidc`, the object store's
and the backup target's keys). Those come from the site's vault.

## How to know it is this

- The machine is lost (disk, host, accidental `k3s-uninstall.sh`): nothing answers, or a new machine
  is ready and empty.
- Or the data is wrong and must go back to last night: a bad migration, an erasure by mistake.
  Everything since the backup is lost by the restore (the recovery point is the last backup, 24 h at
  most with the default schedule). Say so to the people who used it today before you start.

## Before you start

- The S3 endpoint of `backup.target.s3.endpoint` must be reachable from the pods (a target bound to one
  host address, or to the wrong Docker network, leaves the `ship` container hanging with no error).
  An interrupted backup leaves a directory without `COMPLETE`; the restore ignores it.
- Do not stop the writers by hand first: the script does it. If you did (or an earlier run failed), it
  restores the replica counts it saved on each Deployment (`choregos-restore-replicas` annotation), not
  the zeros it finds.

## Which backup

```bash
# The backups, newest last (S3 target; with a volume target: `ls` the volume).
rclone lsf --dirs-only :s3,provider=Other,endpoint=$S3_ENDPOINT:$BUCKET/$PREFIX
```

Only a directory holding `COMPLETE` is a backup; its `MANIFEST` lists the files and their sizes.
Without a stamp the restore takes the newest complete one.

## Restore onto a new machine

1. **k3s** on the new machine, the same version or newer:
   `curl -sfL https://get.k3s.io | sh -`, then `kubectl get storageclass local-path`.
2. **The site's secrets**, from the vault, with the SAME values as before. The run-token key and
   `secrets-keks` above all: with another `secrets-keks` no sealed connector secret opens again.

   ```bash
   kubectl create namespace choregos
   kubectl -n choregos create secret generic choregos-api-secrets \
     --from-file=run-token-private-key=run-token.pem \
     --from-literal=session-secret="$SESSION_SECRET" --from-literal=secrets-keks="$SECRETS_KEKS"
   kubectl -n choregos create secret generic choregos-oidc --from-literal=client-secret="$OIDC_SECRET"
   kubectl -n choregos create secret generic choregos-backup-target \
     --from-literal=access-key="$S3_ACCESS_KEY" --from-literal=secret-key="$S3_SECRET_KEY"
   ```
3. **The chart**, with the same values files as the lost installation:
   `helm install choregos charts/tranibor -n choregos -f charts/tranibor/values/single-node.yaml -f my-site.yaml`.
   Wait until every Deployment is available (`kubectl -n choregos get deploy`). It starts empty; the
   restore replaces that.
4. **The restore**:

   ```bash
   tools/single-node/restore.sh                    # the newest complete backup
   tools/single-node/restore.sh 20261117T020000Z   # or that one
   ```

   It stops every writer (API, console, workers, Temporal, gateway) and leaves the memory running,
   runs the chart's restore job (`kubectl create job --from=cronjob/choregos-restore …`), prints its
   log, and starts the writers again with the replica counts they had (saved in the
   `choregos-restore-replicas` annotation, so a run that follows a failed one still starts them). The
   job drops and re-creates each database from its dump, puts Temporal's file back (`dev` mode), stages
   the memory's backup on Ecphoria's volume and asks Ecphoria to restore it
   (`POST /api/v1/admin/restore`: it checks the digests again and swaps its stores in while it runs),
   then to rebuild its lexical index (`POST /api/v1/admin/reindex`), and copies the object store back
   when the backup holds it.

## Restore in place (same machine, data back to last night)

Skip steps 1 to 3: `tools/single-node/restore.sh [stamp]` on the running installation.

## If the restore fails

The script leaves the writers stopped: starting them over a half-restored database would write on
it. Read the job's log (`kubectl -n choregos logs job/<name> -c fetch|database|files|objects`):

- `no complete backup … nothing restored`: the bucket, prefix or keys of `backup.target` are not
  those of the backups; check `choregos-backup-target` and the values.
- `pg_restore` errors: the database step stops at the first error (`--exit-on-error`); a dump made
  by a newer PostgreSQL than the embedded one is refused. Restore with the chart version that made
  the backup.
- `memory: … Ecphoria answered 403`: the key in `global.memory.tokenSecret` is not an admin key
  (`<key>@:admin` in its `api-keys`); the chart's own `choregos-memory` is one. `… answered 500
  … RESTORE_ERROR`: Ecphoria refused the backup (a digest that does not match, or a manifest newer
  than this Ecphoria: restore with the Ecphoria version that made it); its log says which.
- `memory: this backup holds Ecphoria's files, not Ecphoria's own backup`: see below.

### A backup whose memory is a copy of its files

A backup made before S80-41 holds Ecphoria's files under `memory/` (no `memory/ecphoria/`). They are
good only if Ecphoria was stopped when the backup was taken (the copy then holds no `.wal` beside a
DuckDB file); otherwise they do not replay (#2185). The restore job does not put them back. To put
them back by hand once the rest is restored: `kubectl -n choregos scale deploy/choregos-ecphoria
--replicas=0`, copy the files of `memory/` onto Ecphoria's volume (`choregos-ecphoria` claim) in
place of its own, scale it back to 1, and check its log for `falling back` (there must be none).

Fix the cause and run the script again: every step overwrites what the previous attempt left.

## How to check it is fixed

```bash
kubectl -n choregos get deploy                      # every Deployment available
kubectl -n choregos logs deploy/choregos-api | grep -i "API starting"
kubectl -n choregos logs deploy/choregos-ecphoria | grep -c "falling back"   # must be 0
```

Temporal (`server` mode): its workflows come back with the databases; `temporal workflow list` (see
[temporal-backup.md](temporal-backup.md)) shows those that were running at the backup, and none started
after it. Ecphoria: the restore job's `memory` log prints the counts of the backup's manifest; read
back a record you know was there.

Then in the console: the work items of yesterday are there, in the state they had at the backup. A
work item whose workflow was running resumes from where Temporal's copy left it; one created after the
backup is gone (its tracker issue, if any, is picked up again by the tracker catch-up).

## How it is proven

`tools/single-node/proof.sh` plays this whole runbook from a blank node: install, one work item to
`approved` and one memory record written to Ecphoria, backup (Ecphoria running), the node destroyed,
a new node, install, `restore.sh`, the work item read back `approved` and the record read back. See `docs/plan/STATUS.md` (S80-07b) for when it last ran and what it found.
