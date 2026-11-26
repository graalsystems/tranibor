# Load testing: one node, fakes mode

> **Scope, read first.** Two runs of the local install (`docker compose up`) on one shared 4 vCPU
> machine, **fakes mode** (no model is called): the first on 2026-11-25 with the published 0.29.4
> images, the second on 2026-11-26 with images built from `main` after the two fixes the first run
> asked for (#2284, #2285). They say how many concurrent console users one API process serves before
> it slows, and what breaks first. They do **not** say that the platform is highly available, how it
> behaves on a cluster, with real models, with several nodes, or over hours.
> [What this does not prove](#what-this-does-not-prove) lists the rest.

It answers a gap named by a buyer's review (B6): "no load test". It is a first measurement, not a
capacity statement; the numbers are for this machine and will move on yours.

## The setup

The first run's; [the rerun](#rerun-on-main-2026-11-26-after-2284-and-2285) changes the repository, the
images and the console.

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

## Rerun on `main`, 2026-11-26 (after #2284 and #2285)

The same machine, the same workload and the same command (`--steps 1,5,10,20,50,100`, 120 s a step,
15 s of warm-up, the script's default since the first run), on a fresh install. What changed:

| | |
|:--|:--|
| Repository | `graalsystems/tranibor` at `026bc3561` (`main`, 2026-11-26): the key race fixed (#2284, a per-project advisory lock) and the board batched (#2285, a page's facts read once) |
| Images | **Built here from that commit** (`choregos-api:local`, `choregos-orchestrator:local` from `docker/api.Dockerfile` targets `api` and `worker-local`, `choregos-web:local` through `docker compose build web`; the API container reports build revision `026bc3561`), behind the sandbox's TLS-inspecting proxy through the `build-ca` seam (#2308). One exception, which the measured paths do not run: the SkillSpector stage was taken from the published 0.29.4 API image (`--build-context skillspector=docker-image://ghcr.io/graalsystems/tranibor-api:0.29.4`), because the sandbox's policy refuses the GitHub archive download that stage makes |
| Console | Built from the same commit, it relays to the API (the published 0.29.4 web image could not). The generator still calls the API directly, so the numbers stay comparable; the console's own cost is not measured |

Raw results:
[`tools/load/results/2026-11-26-single-node-main.json`](../tools/load/results/2026-11-26-single-node-main.json).

### Before and after

p95 in milliseconds; "before" is the first run below (0.29.4 images), "after" this one.

| Users | Reads/s before | Reads/s after | Board p95 before | Board p95 after | Inbox p95 before | Inbox p95 after | Creations failed before | Creations failed after |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 2.4 | 3.1 | 393 | 85 | 274 | 220 | 0 % | 0 % |
| 5 | 9.3 | 14.0 | 829 | 147 | 552 | 378 | 0 % | 0 % |
| 10 | 12.7 | 22.5 | 1518 | 312 | 1108 | 747 | 4.7 % | 0 % |
| 20 | 13.0 | 26.3 | 3064 | 765 | 2102 | 1719 | 11.8 % | 0 % |
| 50 | 12.7 | 25.0 | 6074 | 2644 | 5735 | 3691 | 18.5 % | 0 % |
| 100 | 12.8 | 20.5 | 10381 | 7432 | 10096 | 9664 | 19.4 % | 0 % |

* **Throughput about doubles**: the five reads plateau at about 25 requests per second from 20 users
  on, against 13 from 10 users on.
* **The board is no longer the slowest route.** At one user its p50 went from 330 ms to 59 ms
  (one work item: 28 ms); at 10 users its p95 from 1.5 s to 0.31 s.
* **No creation failed** in any step (the first run lost 25 to the key race), and no read failed up
  to 50 users. At 100 users 5 reads of 2,456 answered 500 (0.2 %, against 4.5 %): connection pool
  timeouts, `QueuePool limit of size 10 overflow 5 reached` (10 in the API's log, 18 in the worker's).
* **The knee moved from about 10 to about 20 users.** At 10 users every read is under one second at
  p95 (the slowest, the inbox, at 0.75 s). At 20 users the inbox crosses it (1.7 s) while the board
  is at 0.77 s. **`/me/pending` is now the slowest hot route**: it keeps a per-item pattern (each
  waiting request asks who may answer it and whether its mark is readable), named in the row
  `board-batched-reads` and not fixed.
* **PostgreSQL is now the busiest container**: 117 % CPU at 10 users, 145 to 158 % from 20 on, while
  the API process stays at one core (93 to 105 %). The next limits are the API's single process and
  the database's CPU, not a per-item query.

All routes, all steps (p50 / p95 / p99 in ms, errors included):

| Users | Route | Requests | req/s | Errors % | p50 ms | p95 ms | p99 ms |
|--:|:--|--:|--:|--:|--:|--:|--:|
| 1 | board (work items list) | 107 | 0.89 | 0.0 | 59 | 85 | 123 |
| 1 | one work item | 64 | 0.53 | 0.0 | 28 | 38 | 53 |
| 1 | inbox `/me/pending` | 80 | 0.67 | 0.0 | 181 | 220 | 341 |
| 1 | runs of a work item | 71 | 0.59 | 0.0 | 20 | 27 | 39 |
| 1 | findings | 53 | 0.44 | 0.0 | 21 | 43 | 53 |
| 1 | create a work item | 10 | 0.08 | 0.0 | 62 | 110 | 110 |
| 5 | board (work items list) | 488 | 4.07 | 0.0 | 80 | 147 | 302 |
| 5 | one work item | 344 | 2.87 | 0.0 | 36 | 81 | 129 |
| 5 | inbox `/me/pending` | 333 | 2.77 | 0.0 | 244 | 378 | 483 |
| 5 | runs of a work item | 249 | 2.08 | 0.0 | 28 | 79 | 106 |
| 5 | findings | 266 | 2.22 | 0.0 | 28 | 64 | 143 |
| 5 | create a work item | 51 | 0.42 | 0.0 | 82 | 156 | 200 |
| 10 | board (work items list) | 848 | 7.07 | 0.0 | 143 | 312 | 475 |
| 10 | one work item | 531 | 4.42 | 0.0 | 70 | 168 | 365 |
| 10 | inbox `/me/pending` | 530 | 4.42 | 0.0 | 429 | 747 | 874 |
| 10 | runs of a work item | 372 | 3.1 | 0.0 | 60 | 145 | 321 |
| 10 | findings | 418 | 3.48 | 0.0 | 65 | 151 | 275 |
| 10 | create a work item | 76 | 0.63 | 0.0 | 203 | 552 | 723 |
| 20 | board (work items list) | 950 | 7.92 | 0.0 | 388 | 765 | 989 |
| 20 | one work item | 609 | 5.08 | 0.0 | 224 | 537 | 645 |
| 20 | inbox `/me/pending` | 657 | 5.47 | 0.0 | 1138 | 1719 | 1917 |
| 20 | runs of a work item | 481 | 4.01 | 0.0 | 199 | 519 | 627 |
| 20 | findings | 452 | 3.77 | 0.0 | 213 | 518 | 599 |
| 20 | create a work item | 93 | 0.78 | 0.0 | 473 | 943 | 1186 |
| 50 | board (work items list) | 871 | 7.26 | 0.0 | 1578 | 2644 | 3279 |
| 50 | one work item | 603 | 5.03 | 0.0 | 1308 | 2150 | 2891 |
| 50 | inbox `/me/pending` | 645 | 5.38 | 0.0 | 2664 | 3691 | 4453 |
| 50 | runs of a work item | 444 | 3.7 | 0.0 | 1319 | 2222 | 3296 |
| 50 | findings | 430 | 3.58 | 0.0 | 1351 | 2299 | 2867 |
| 50 | create a work item | 98 | 0.82 | 0.0 | 1772 | 3014 | 3637 |
| 100 | board (work items list) | 724 | 6.03 | 0.0 | 4324 | 7432 | 8872 |
| 100 | one work item | 493 | 4.11 | 0.2 | 4134 | 7829 | 9007 |
| 100 | inbox `/me/pending` | 497 | 4.14 | 0.6 | 6325 | 9664 | 11107 |
| 100 | runs of a work item | 374 | 3.12 | 0.27 | 4027 | 7259 | 8891 |
| 100 | findings | 368 | 3.07 | 0.0 | 4035 | 5505 | 8361 |
| 100 | create a work item | 80 | 0.67 | 0.0 | 4500 | 5578 | 8484 |

### What the platform did with the work items (rerun)

| Users | Created | Approved (decisions) | Approvals per minute | Creation to `awaiting_approval`, p50 / p95 |
|--:|--:|--:|--:|:--|
| 1 | 10 | 38 | 19.0 | 49.6 s / 52.9 s |
| 5 | 51 | 51 | 25.5 | 1.7 s / 2.6 s |
| 10 | 76 | 76 | 38.0 | 2.4 s / 4.6 s |
| 20 | 93 | 91 | 45.5 | 4.1 s / 7.2 s |
| 50 | 98 | 64 | 32.0 | 18.6 s / 50.6 s |
| 100 | 80 | 36 | 18.0 | 101.2 s / 173.2 s |

Creations follow the users now that none fails (98 at 50 users against 44). From 50 users on the one
approver falls behind: its poll and its decisions wait in the same queue as the users' reads (one poll
took 4.6 s at 100 users), so items wait longer in `awaiting_approval`; this is the generator's single
approver, not a ceiling of the workflow. After the 30 s drain: 360 `approved`, 80 `awaiting_approval`,
33 `needs_human` (the budget note below). The 34 refused decisions are the approver's own 409 (`no
human request is pending`: the state reads `awaiting_approval` a moment before the request exists),
asked again at its next poll.

### What the containers cost (rerun)

| Users | api CPU % mean (max) | postgres CPU % mean (max) | temporal CPU % mean (max) | worker CPU % mean (max) | web CPU % mean (max) | api MiB max | postgres MiB max |
|--:|:--|:--|:--|:--|:--|--:|--:|
| 1 | 29 (45) | 16 (36) | 11 (30) | 11 (53) | 9 (23) | 727 | 317 |
| 5 | 57 (83) | 68 (86) | 19 (29) | 20 (40) | 7 (24) | 719 | 336 |
| 10 | 93 (120) | 117 (137) | 30 (52) | 23 (55) | 7 (23) | 722 | 386 |
| 20 | 100 (125) | 145 (175) | 30 (49) | 26 (52) | 12 (28) | 720 | 400 |
| 50 | 102 (135) | 158 (190) | 31 (50) | 29 (45) | 6 (22) | 717 | 423 |
| 100 | 105 (125) | 155 (191) | 30 (56) | 21 (41) | 10 (25) | 736 | 432 |

The API process held about 720 MiB from the first step, against 224 to 257 MiB for the 0.29.4 image:
the image is a different build of more recent code, and the difference was not investigated.

### Bursts (rerun)

On a fresh install, with nothing else running and **no patch hot-copied** (the first run's bursts
needed one):

| Burst | Created | Decided within 300 s | After both bursts |
|:--|:--|:--|:--|
| 40 work items, 4 at a time | 40 of 40 in 3.1 s | 34 | |
| 100 work items, 10 at a time (same install, after the first) | **100 of 100 in 6.4 s** (0.29.4 without the patch: 16) | 39 | 88 `approved`, 67 `needs_human`, 1 `received` (with the warm-ups' items) |

The creations hold under concurrency now. The workflow's rate is still not measured: the work items
that did not reach a decision were parked in `needs_human` by the daily budget, all 67 with the same
reason, "the stage `plan` did not start: 0.00 $ left (bound by project_day; stage 8.00 $, ticket
25.00 $, project_day 0.00 $)".

**The budget note, explained.** What is left under the project's daily cap is the cap minus the day's
spend **minus the caps of the gateway keys still open** (`open_reservations`,
`apps/api/src/tranibor_api/services/budgets.py`, read by the stage's reservation in
`apps/orchestrator/src/tranibor_orchestrator/activities/stage_refusals.py`). Every stage in flight
holds a key reserved at its stage cap (8 $ for `plan`), spent or not: in fakes mode nothing is spent,
yet once enough stages are in flight their reservations use up the day, and the next stage is refused
and parked in `needs_human` instead of waiting for a reservation to close. The keys are released as
their runs end (after the bursts, all 88 were revoked, none left open). So this is the budget guard
doing what it says, under a burst far above what the demo project's policy allows in flight; whether
a refused stage should wait instead of parking is a product question, not a load defect.

## First run, 2026-11-25 (published 0.29.4 images)

The numbers below are the first run's, before #2284 and #2285; the setup table above describes it.
The knee and the bottlenecks that follow are the first run's too, each with what the rerun changed.

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
API container; the patch is not in the repository (the fix that replaced it, #2284, is a per-project
advisory lock).** Without it, the same 100-item burst created 16:

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

Defined as the concurrency where the p95 of a hot read passes one second, or errors appear. In the
first run (below) it was about 10 users; in the rerun on `main` it is **about 20 users**, the inbox
first, with no creation failing.

* **About 10 users.** At 10 users the board's p95 is 1.5 s and the API process is at 113 % CPU: one
  core, saturated; the inbox is at 1.1 s. Below, at 5 users, every route is under 1 s at p95 and
  nothing fails. The cheap reads (`work item`, `runs`, `findings`) hold under one second at p95 up to
  20 users (0.75 to 0.93 s) and cross it at 50 (3.6 to 4.7 s).
* **Errors start at 100 users** on reads (4.5 %), and earlier on creation (4.7 % at 10 users, 12 % at
  20): that one is a race, not saturation (bottleneck 4, fixed since by #2284).
* **A sensible reading for this machine:** a single API process answers about 13 requests per second
  of this mix, which is about 10 users who each act every few seconds. It is a floor for this
  machine under a generator that steals CPU from it, not a ceiling for a production node.

## Bottlenecks seen

**1. The board was an N+1 (the slowest hot route, and the cause of most of the CPU).** On an idle
install, one `GET /projects/demo:demo/work-items` of 21 work items takes about 150 ms (`curl`), and
`/me/pending` about 40 ms. With PostgreSQL's statement log on, that one board request ran 4 queries
per work item (`workflow_defs`, `runs`, `human_requests`, `cost_ledger`: 21 each), 84 round trips on
top of the page's own, from `work_item_dto` (`apps/api/src/tranibor_api/services/tickets.py`), called
once per item by `list_work_items`; there are more for the repository view and the cost estimate. The
page size is 50, so a full page is about 200 queries. At 1 user the board's p50 is 330 ms against 32
ms for one work item. **Fixed since by #2285** (status row `board-batched-reads`): the page reads each
kind once for all its work items (`work_item_dtos`), so a page reads the same number of statements
whatever its size: 14 on SQLite for a page of 4, 40 or 50 work items (16 for a member who does not
read a restricted mark), where it read 270 for 50; a test guards it
(`apps/api/tests/test_board_reads_a_page_at_once.py`). Measured in the rerun on `main`: the board's
p50 at one user went from 330 ms to 59 ms, its p95 at 10 users from 1.5 s to 0.31 s, and the reads'
throughput from about 13 to about 25 requests per second.

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

**4. Concurrent creations on one project failed with HTTP 500 (found here, fixed since, #2284).**
`internal_work_item_key` read `max(n) + 1` over the project's keys and the unique constraint
`project_tracker_key` was the only guard: two requests in flight took the same number and the second
got an `IntegrityError` as a 500. On the real PostgreSQL, **10 creations in flight at once on one
project: 84 of 100 answered 500** (16 created); in the main run, 25 creations failed that way. A
candidate patch, tried on the running containers only (insert inside `session.begin_nested()` and read
the next number again on `IntegrityError`), created 100 of 100, but was not kept: on SQLite releasing
the outermost savepoint commits the transaction, and
`apps/api/tests/test_work_item_files.py::test_start_true_on_a_workflow_that_requires_files_is_refused`
went red. **The fix (#2284)** serialises the minting per project on PostgreSQL: `lock_key_minting`
(`apps/api/src/tranibor_api/services/tickets.py`) takes a `pg_advisory_xact_lock` on the project
before the keys are read, held until the transaction commits or rolls back, so the next creation reads
the number this one inserted; nothing changes on SQLite. It covers every path that mints a number (the
route, the MCP and A2A doors, triggers without a key, actions, and the orchestrator's findings through
`internal_ticket_key`). `apps/api/tests/test_work_item_key_concurrency.py` proves it on PostgreSQL: 20
creations at once through the route, then 20 findings minted at once in their own sessions, each get
a distinct key with no error; without the lock 18 of 20 answered 500. In the rerun on `main`, none of the 408 creations of
the steps failed and the burst of 100, 10 at a time, created 100 of 100 with no patch. **The workaround (one creation
at a time, or retry a 500) is no longer needed.** The lock is held from the minting to the commit: two
creations on one project now take turns for that span, which includes the routing model's answer when
the project's strategy is `llm`; creations on different projects do not wait on each other.

The 118 "unknown work item" tracebacks the worker logged in the main run were traced to another cause:
the interpreter is started inside the creating transaction, before its commit, and the worker's first
activity (`load_context`) can read before the row is visible, then retries (2 s, at most 5 attempts).
The key race does not start interpreters: a creation that loses it fails at its `INSERT`, before the
start (checked on PostgreSQL without the lock: 2 created, 2 interpreters started, none on a row that
was not committed). It is a finding of its own, #2291; it is not fixed here.

**5. Not a performance failure: `needs_human` on a budget refusal.** Under a burst of 100, 41
work items ended in `needs_human` with this event: "the stage `plan` did not start: 0.00 $ left
(bound by project_day; stage 8.00 $, ticket 25.00 $, project_day 0.00 $)". In fakes mode nothing is
spent, so this is the day cap of the demo project refusing to reserve a stage's budget while many
stages are in flight. What holds the reservations was read in the rerun: the open gateway keys of the
stages in flight, each at its stage cap ([the budget note](#bursts-rerun)); the cap's value was not
read, so "how many work items a project runs at once in fakes mode" stays unmeasured. The 3 stuck
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
* **A long run.** Each step is 2 minutes, each run about 15 minutes: no memory growth over
  hours, no table growth (about 270 work items in the first run, 470 in the rerun), no vacuum or
  retention behaviour.
* **The console.** The Next.js server and the browser were not in the path, in either run (the rerun's
  console, built from `main`, relays to the API, but the generator called the API directly); neither
  were SSE streams (`/events/stream`), uploads, or the MCP and A2A doors.
* **Every line of the image.** The rerun's images are built from `026bc3561`, except the SkillSpector
  stage, taken from the published 0.29.4 image (the sandbox refuses its download); no measured route
  runs it.
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
