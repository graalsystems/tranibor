# Load testing: one node, fakes mode

> **Scope, read first.** One run of the local install (`docker compose up`) on one shared 4 vCPU
> machine, **fakes mode** (no model is called), measured on 2026-11-25. It says how many concurrent
> console users one API process serves before it slows, and what breaks first. It does **not** say
> that the platform is highly available, how it behaves on a cluster, with real models, with several
> nodes, or over hours. [What this does not prove](#what-this-does-not-prove) lists the rest.

It answers a gap named by a buyer's review (B6): "no load test". It is a first measurement, not a
capacity statement; the numbers are for this machine and will move on yours.

## The setup

| | |
|:--|:--|
| Machine | 4 vCPU (Intel Xeon @ 2.10 GHz), 16 GB RAM, a shared VM (a micro-VM sandbox: no promise on noisy neighbours) |
| Docker | Engine 29.10.0, Compose 5.7.1 |
| Repository | `graalsystems/tranibor` at `61d0b132e` (`main`, 2026-11-25); the load script is that commit's |
| Images | **The published 0.29.4 images** (`ghcr.io/graalsystems/tranibor-{api,orchestrator,web}:0.29.4`, API digest `sha256:979defbfbaf9cc3e53a1f953806e2d0d4bccd0d733362d025658c942afd00041`), tagged as the compose file expects (`choregos-api:local`, ...), then `docker compose up -d --no-build`. The sandbox cannot build the images (the build's package downloads do not go through its proxy), so these are the release's code, not necessarily every line of the commit. The workers' image is the published orchestrator image plus the Docker CLI layer the `worker-local` target adds |
| Mode | `.env` from `.env.example`, no model key: **fakes**. Each agent stage returns what its transition declares and says that no model ran. Temporal, PostgreSQL, the gates and the human decision are the real path. No model latency is in any number below |
| Topology | PostgreSQL 16 (with the compose file's `fsync=off`), Temporal `auto-setup` on the same PostgreSQL, **one API process** (`uvicorn`, no `--workers`), one worker container, LiteLLM, RustFS, the console |
| Load generator | `tools/load/single_node_load.py`, on **the same machine**: it competes with the platform for the 4 vCPUs |
| What was called | The API on `localhost:8000`, directly, with a bearer token created through the API. The console (Next.js) was **not** in the path: the published web image of this release bakes the API address `127.0.0.1:8000` into its relay, which a container cannot reach |

## The workload

Each virtual user loops: one request, then a think time of 250 ms on average (0.125 to 0.375 s).
97 % of the requests are reads of the console's hottest routes, weighted 30/20/20/15/15:

1. the board: `GET /projects/demo:demo/work-items` (a full page of 50, the page size);
2. one work item: `GET /work-items/{id}`;
3. the inbox: `GET /me/pending`;
4. the runs of a work item: `GET /work-items/{id}/runs`;
5. the findings: `GET /projects/demo:demo/findings`.

3 % of the iterations create a work item (`POST /projects/demo:demo/work-items`, the default
workflow `local-demo`: an agent stage, then a human approval). One approver (a signed-in session, 2 s
poll: the API refuses a decision made with a token) lists the work items in `awaiting_approval` and
approves them, so that the fake agents' workflows run to their end. 60 work items are created before
the first step so that the board is a full page in every step. Steps of 1, 5, 10, 20, 50 and 100
users, 120 s each, after 30 s of unrecorded warm-up. Raw results:
[`tools/load/results/2026-11-25-single-node.json`](../tools/load/results/2026-11-25-single-node.json).

Reproduce it:

```bash
cp .env.example .env && docker compose up -d
uv run python tools/load/single_node_load.py --steps 1,5,10,20,50,100 --json results.json
uv run python tools/load/single_node_load.py --steps "" --seed-items 0 --burst 100 --burst-concurrency 10
```

Why a Python script and not k6: `httpx` is already a dependency of the platform (no new image, no new
pin), the sign-in and the decision need a cookie session beside the token, and the script reads
`docker stats` in the same loop. k6 would add a pinned image to the lock for the same arithmetic.

## Results

### What the users see

p50 / p95 / p99 are in milliseconds, over every answered request of the step, errors included (a
request that times out counts at its timeout).

| Users | Route | Requests | req/s | Errors % | p50 ms | p95 ms | p99 ms |
|--:|:--|--:|--:|--:|--:|--:|--:|
| 1 | board (work items list) | 75 | 0.62 | 0.0 | 330 | 393 | 623 |
| 1 | one work item | 55 | 0.46 | 0.0 | 32 | 43 | 53 |
| 1 | inbox `/me/pending` | 65 | 0.54 | 0.0 | 220 | 274 | 402 |
| 1 | runs of a work item | 56 | 0.47 | 0.0 | 24 | 46 | 52 |
| 1 | findings | 42 | 0.35 | 0.0 | 29 | 62 | 83 |
| 1 | create a work item | 7 | 0.06 | 0.0 | 67 | 194 | 194 |
| 5 | board (work items list) | 329 | 2.74 | 0.0 | 565 | 829 | 1095 |
| 5 | one work item | 239 | 1.99 | 0.0 | 62 | 124 | 142 |
| 5 | inbox `/me/pending` | 212 | 1.77 | 0.0 | 361 | 552 | 614 |
| 5 | runs of a work item | 157 | 1.31 | 0.0 | 51 | 97 | 128 |
| 5 | findings | 176 | 1.47 | 0.0 | 59 | 120 | 305 |
| 5 | create a work item | 35 | 0.29 | 0.0 | 126 | 243 | 248 |
| 10 | board (work items list) | 430 | 3.58 | 0.0 | 1111 | 1518 | 1657 |
| 10 | one work item | 329 | 2.74 | 0.0 | 146 | 274 | 448 |
| 10 | inbox `/me/pending` | 287 | 2.39 | 0.0 | 697 | 1108 | 1228 |
| 10 | runs of a work item | 234 | 1.95 | 0.0 | 121 | 200 | 374 |
| 10 | findings | 239 | 1.99 | 0.0 | 140 | 248 | 326 |
| 10 | create a work item | 43 | 0.36 | 4.65 | 256 | 386 | 601 |
| 20 | board (work items list) | 455 | 3.79 | 0.0 | 2499 | 3064 | 3370 |
| 20 | one work item | 327 | 2.73 | 0.0 | 376 | 829 | 1079 |
| 20 | inbox `/me/pending` | 313 | 2.61 | 0.0 | 1576 | 2102 | 2480 |
| 20 | runs of a work item | 228 | 1.9 | 0.0 | 323 | 754 | 1208 |
| 20 | findings | 239 | 1.99 | 0.0 | 367 | 926 | 1357 |
| 20 | create a work item | 51 | 0.42 | 11.76 | 585 | 1173 | 1618 |
| 50 | board (work items list) | 444 | 3.7 | 0.0 | 4957 | 6074 | 7428 |
| 50 | one work item | 285 | 2.38 | 0.0 | 2633 | 3598 | 4389 |
| 50 | inbox `/me/pending` | 341 | 2.84 | 0.0 | 4112 | 5735 | 7067 |
| 50 | runs of a work item | 235 | 1.96 | 0.0 | 2719 | 3989 | 5499 |
| 50 | findings | 214 | 1.78 | 0.0 | 2758 | 4708 | 5354 |
| 50 | create a work item | 54 | 0.45 | 18.52 | 2904 | 3962 | 6312 |
| 100 | board (work items list) | 514 | 4.28 | 3.7 | 9130 | 10381 | 11037 |
| 100 | one work item | 304 | 2.53 | 4.28 | 6886 | 8747 | 10152 |
| 100 | inbox `/me/pending` | 302 | 2.52 | 5.3 | 8523 | 10096 | 10144 |
| 100 | runs of a work item | 195 | 1.62 | 5.13 | 6692 | 10079 | 10114 |
| 100 | findings | 217 | 1.81 | 5.07 | 6922 | 10072 | 10115 |
| 100 | create a work item | 36 | 0.3 | 19.44 | 7821 | 10128 | 10131 |

Total throughput of the five reads, in requests per second: 2.4 (1 user), 9.3 (5), 12.7 (10), 13.0
(20), 12.7 (50), 12.8 (100). **It stops growing at about 10 users**: from there more users only wait
longer. Error rate of the reads: 0 % up to 50 users, **4.5 %** at 100 (the HTTP 500s are connection
pool timeouts, see below).

### What the platform does with the work items

| Users | Created | Approved (decisions) | Approvals per minute | Creation to `awaiting_approval`, p50 / p95 |
|--:|--:|--:|--:|:--|
| 1 | 7 | 33 | 16.5 | 54.4 s / 58.7 s |
| 5 | 35 | 35 | 17.5 | 2.0 s / 3.5 s |
| 10 | 41 | 40 | 20.0 | 3.4 s / 5.7 s |
| 20 | 45 | 44 | 22.0 | 4.7 s / 7.6 s |
| 50 | 44 | 35 | 17.5 | 25.2 s / 63.9 s |
| 100 | 29 | 34 | 17.0 | 78.1 s / 120.2 s |

"Approved (decisions)" counts the approvals the API accepted in the step, "Creation to
`awaiting_approval`" is read by the approver's poll (2 s resolution) and includes the wait behind work
items created earlier. At the end of the run, after a 30 s drain, the project held 223 `approved`, 17
`awaiting_approval` and 29 `needs_human` work items (see the budget note below). Approvals per minute
follow the creation rate (3 % of the iterations), they are not a ceiling.

The platform's own throughput was measured separately, on a fresh install with nothing else running
(`--burst`). **These two bursts ran with a candidate patch for bottleneck 4 below hot-copied into the
API container; the patch is not in the repository.** Without it, the same 100-item burst created 16:

| Burst | Created | Decided | Terminal (`approved`) | Rate |
|:--|:--|:--|:--|:--|
| 40 work items, 4 at a time | 40 of 40 in 4.4 s | all, 16.7 s after the start | all, 18.2 s | **132 work items/min** |
| 100 work items, 10 at a time (after the first burst, same install) | 100 of 100 in 10.4 s | 56 of 100 within 300 s, then no more | after both bursts (140 created): 97 `approved`, 3 `awaiting_approval`, 41 `needs_human` | not a rate: see the budget note |

### What the containers cost

CPU is `docker stats` (100 % is one core, the machine has 400 %); memory is the largest sample.

| Users | api CPU % mean (max) | postgres CPU % mean (max) | temporal CPU % mean (max) | worker CPU % mean (max) | api MiB max | postgres MiB max |
|--:|:--|:--|:--|:--|--:|--:|
| 1 | 37 (66) | 27 (78) | 15 (56) | 6 (48) | 224 | 285 |
| 5 | 90 (113) | 64 (94) | 16 (43) | 11 (44) | 227 | 300 |
| 10 | 113 (133) | 93 (115) | 20 (63) | 18 (66) | 226 | 340 |
| 20 | 110 (134) | 100 (128) | 20 (51) | 16 (47) | 243 | 374 |
| 50 | 109 (133) | 98 (132) | 21 (60) | 19 (59) | 250 | 378 |
| 100 | 119 (132) | 97 (119) | 13 (27) | 7 (29) | 257 | 378 |

LiteLLM held about 600 MiB and the object store about 110 MiB throughout, nearly idle.

## The knee

Defined as the concurrency where the p95 of a hot read passes one second, or errors appear.

* **About 10 users.** At 10 users the board's p95 is 1.5 s and the API process is at 113 % CPU: one
  core, saturated; the inbox is at 1.1 s. Below, at 5 users, every route is under 1 s at p95 and
  nothing fails. The cheap reads (`work item`, `runs`, `findings`) hold under one second at p95 up to
  20 users (0.75 to 0.93 s) and cross it at 50 (3.6 to 4.7 s).
* **Errors start at 100 users** on reads (4.5 %), and earlier on creation (4.7 % at 10 users, 12 % at
  20): that one is a race, not saturation (bottleneck 4).
* **A sensible reading for this machine:** a single API process answers about 13 requests per second
  of this mix, which is about 10 users who each act every few seconds. It is a floor for this
  machine under a generator that steals CPU from it, not a ceiling for a production node.

## Bottlenecks seen

**1. The board is an N+1 (the slowest hot route, and the cause of most of the CPU).** On an idle
install, one `GET /projects/demo:demo/work-items` of 21 work items takes about 150 ms (`curl`), and
`/me/pending` about 40 ms. With PostgreSQL's statement log on, that one board request ran 4 queries
per work item (`workflow_defs`, `runs`, `human_requests`, `cost_ledger`: 21 each), 84 round trips on
top of the page's own, from `work_item_dto` (`apps/api/src/tranibor_api/services/tickets.py`), called
once per item by `list_work_items`; there are more for the repository view and the cost estimate. The
page size is 50, so a full page is about 200 queries. At 1 user the board's p50 is 330 ms against 32
ms for one work item. Not fixed here: batching the per-item reads is not a small change, and it
touches the DTO every route shares. **A finding for a story of its own.**

**2. One API process and one core.** The API container runs one `uvicorn` process; its CPU sits at
110 to 119 % from 10 users on, PostgreSQL at about 100 %. Both are saturated, on a machine that also
runs the generator. More API replicas (or `--workers`) and a second core for PostgreSQL would move
the knee; this run did not try them.

**3. The connection pool is the failure mode, not the first limit.** `db_pool_size=10` and
`db_max_overflow=5` (`apps/api/src/tranibor_api/config.py`), with a 10 s wait. Once requests queue
behind the CPU, they hold their connections, the 16th waits 10 s and answers HTTP 500
(`QueuePool limit of size 10 overflow 5 reached`): 76 such errors in the first run, and the worker
logged the same error on its own pool (20 concurrent activities against a pool of 15; the worker's
concurrency is a setting since S80-07a). The pool was not changed here: raising it moves the queue to
PostgreSQL, which was already saturated.

**4. Concurrent creations on one project fail with HTTP 500 (found, not fixed here).**
`internal_work_item_key` reads `max(n) + 1` over the project's keys and the unique constraint
`project_tracker_key` is the only guard: two requests in flight take the same number and the second
gets an `IntegrityError` as a 500. On the real PostgreSQL, **10 creations in flight at once on one
project: 84 of 100 answered 500** (16 created); in the main run, 25 creations failed that way, and the
worker logged 118 "unknown work item" tracebacks (the cause of those was not traced: they stop with the
patch below, which suggests the same collisions). A candidate patch, tried on the running containers
only: insert in `create_work_item` inside `session.begin_nested()` and read the next number again on
`IntegrityError`, five times at most. With it, 100 of 100 were created, with no 500 and no "unknown
work item". It is **not included**, because it is not safe as it stands: on SQLite (development and
the unit tests) releasing the outermost savepoint commits the transaction, and
`apps/api/tests/test_work_item_files.py::test_start_true_on_a_workflow_that_requires_files_is_refused`
(a refused creation must leave nothing) went red. A fix wants its own story: a PostgreSQL-only guard
(an advisory lock per project, as `budgets.lock_budget_scopes` does for budgets, made re-entrant) and a
test on a real PostgreSQL with concurrent sessions. Until then, **create work items on one project one
at a time, or retry a 500**.

**5. Not a performance failure: `needs_human` on a budget refusal.** Under a burst of 100, 41
work items ended in `needs_human` with this event: "the stage `plan` did not start: 0.00 $ left
(bound by project_day; stage 8.00 $, ticket 25.00 $, project_day 0.00 $)". In fakes mode nothing is
spent, so this is the day cap of the demo project refusing to reserve a stage's budget while many
stages are in flight. The cap's value and exactly what holds the reservations were not read: treat
"how many work items a project runs at once in fakes mode" as unmeasured. The 3 stuck
`awaiting_approval` were not explained either (a decision accepted by hand later did go through).

## What this does not prove

* **High availability.** One API process, one worker, one PostgreSQL, one Temporal on one machine.
  Nothing was killed, restarted or failed over during the load.
* **A real cluster.** Docker Compose on a VM, not Kubernetes: no ingress, no network between nodes,
  no CNPG, no Temporal cluster, no resource limits.
* **Real models and real agents.** Fakes mode: no model latency, no tool call, no runner container,
  no spend, no object-store artifacts of size. A real stage lasts minutes and holds a worker slot for
  that time: the work items per minute here say nothing about it.
* **Several nodes, replicas or a bigger machine.** The knee is one process on 4 shared vCPUs.
* **A long run.** Each step is 2 minutes, the whole run about 15 minutes: no memory growth over
  hours, no table growth (about 270 work items), no vacuum or retention behaviour.
* **The console.** The Next.js server and the browser were not in the path; neither were SSE streams
  (`/events/stream`), uploads, or the MCP and A2A doors.
* **Other mixes.** A different ratio of reads and writes, other routes (audit export, costs,
  knowledge), other organisation sizes, or many organisations at once.
* **A clean machine.** The generator and the platform share the CPUs, and `fsync` is off in the
  compose file's PostgreSQL. The steps ran in one go on a database that kept growing (the board was
  already a full page).

A first run of the main steps (5, 20, 50, 100 users, two approvers polling every 0.5 s, so more load of
its own) had the same ceiling, about 12 requests per second from 5 users on, and the same failure
modes; the run reported above polls less, to leave the capacity to the users. Its numbers are not kept.

One more caveat on method: the approver did not retry a 409 in the main run (6 approvals lost of
about 280); the script now asks again at the next poll, which is what the burst runs used. It does
not move the latencies.
