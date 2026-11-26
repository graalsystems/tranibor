# Ecphoria lost its Raft quorum

## How to know it is this

- The quorum alert, or `choregos_context_pack_empty_total` climbing.
- Runs carry on: **that is normal and intended** — without memory, the context pack is empty.

## Understand

This page is for an Ecphoria an operator runs clustered (a three-replica StatefulSet, with the
restore CronJob its own repository defines: the chart renders neither). The chart's embedded Ecphoria
(`global.memory.embedded`) is **one replica on one volume**: it has no quorum to lose. If that one is
broken, its restore is the memory step of `single-node-restore.md` (`POST /api/v1/admin/restore` on a
backup taken by `POST /api/v1/admin/backup`, S80-41).

Ecphoria here runs as a three-replica StatefulSet with Raft. One lost node is tolerated; with
two, the quorum falls and writes stop.

```bash
kubectl -n choregos-memory get pods -l app.kubernetes.io/name=ecphoria
kubectl -n choregos-memory logs ecphoria-0 | grep -i raft
```

## Act

1. **One node lost**: let it come back. If its PVC is corrupt, delete it:
   ```bash
   kubectl -n choregos-memory delete pvc data-ecphoria-2
   kubectl -n choregos-memory delete pod ecphoria-2
   ```
   The node resynchronises from the leader.
2. **Quorum lost (two nodes)**: restore from the daily backup.
   ```bash
   kubectl -n choregos-memory create job --from=cronjob/ecphoria-restore restore-$(date +%s)
   ```
3. **Meanwhile**: nothing to do on the platform side. The adapter's circuit breaker returns
   empty packs in under 300 ms, and stages run without memory.

## Check it is fixed

```bash
curl -s http://ecphoria.choregos-memory:8432/health | jq
tranibor items show <ticket>   # the next runs have context again
```

## Do not

- Switch to `pgvector` in a panic: that is a considered configuration decision (ADR-0007),
  not an emergency move. The facts written into Ecphoria would not be there.
