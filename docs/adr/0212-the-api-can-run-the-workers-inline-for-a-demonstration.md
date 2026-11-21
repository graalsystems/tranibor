# 0212 — The API can run the workers inline, for a demonstration

- **Status**: accepted, 2026-11-21
- **Concerns**: `apps/api` (`demo_inline.py`, the lifespan), `apps/orchestrator` (`worker.build_workers`),
  `packages/core` (`production.refuse_demo_inline_worker`); builds on [ADR 0005](0005-adapters-and-fakes.md)
  and [ADR 0008](0008-idempotence-and-run-id.md)

## Context

On a laptop, the API run alone under `TRANIBOR_FAKES=1` (SQLite, no Temporal) accepts a human decision
(`POST /api/v1/work-items/{id}/decisions` answers 202) and nothing moves: under the fakes the API's
Temporal gateway is `FakeTemporal`, which records starts and signals and runs nothing. A pre-sales
demonstration in front of an audience is a frozen state.

What exists, and why it does not serve:

- `make demo` (`tranibor_orchestrator.demo`) plays a ticket in one process with the **local interpreter**
  (`local.run_local`), calling the real activities and no Temporal. It is a scripted scenario that prints
  a trace: it serves no HTTP, and a decision is a callback of the script, not a request.
- `docker compose up` and `make dev-up` run the whole stack (PostgreSQL, Temporal, the workers): right for
  a trial, too heavy for "start the API and show".

## Decision

1. **An explicit opt-in starts the orchestrator's workers inside the API process.**
   `TRANIBOR_DEMO_INLINE_WORKER=1` makes the API's lifespan start Temporal's in-memory test server
   (`WorkflowEnvironment.start_time_skipping()`) and the workers the chart deploys, one per queue
   (`tranibor_orchestrator.worker.build_workers`, the function `run_worker` now uses too), and replace
   the gateway by `InlineTemporal`, the real `RealTemporal` on that server's client. The interpreter
   workflow, its activities, its signals and its id policies are the product's: no workflow logic is
   duplicated, and a decision is delivered by the same signal as in production.
2. **The simulated agent is the fakes' own.** One shared `AdapterSet.fakes()` is the adapters' override
   (as `make demo` does), so the `FakeExecutor`'s default result (done, with every declared output as a
   placeholder) answers each agent stage, and the human gates wait for a person. The fake tracker accepts
   the tickets the API creates (it is scripted by `seed()` otherwise).
3. **It never activates implicitly and is refused elsewhere.** Absent, nothing changes. On, the API
   refuses to start unless `TRANIBOR_FAKES` is on **and** `TRANIBOR_ENV` is `dev` or `test`
   (`refuse_demo_inline_worker`, checked before anything else starts; `staging` and `prod` are refused,
   so is a dev API on real adapters), naming the setting to change.
4. **The package boundary stays one way.** The API imports the orchestrator lazily, only in this mode;
   the `api` image carries no orchestrator, and the mode then stops with a message saying so. The core
   never depends on it.

## Limits

- **No durability, no recovery**: the server is in memory, every workflow dies with the process. This is
  not Temporal's guarantee (ADR 0008's replay protects nothing here) and must never be offered as an
  install.
- **Time is not skipped.** The test server skips time only while a client awaits a result, which the API
  never does: SLAs and timers run in wall-clock time, so an escalation after hours will not happen in front
  of an audience.
- **The simulated agent says "done" with placeholders**: it shows the governance and the flow, not what a
  real agent produces. A scripted story (a finding, a rejected gate) needs a handler on the `FakeExecutor`,
  which this mode does not offer; `make demo` stays the way to show that.
- **The server binary** is the one `temporalio` downloads on first use (cached afterwards): a first start
  needs the network, or a pre-seeded cache.
- **Nothing is real**: one tracker, one repository, one executor, all fakes; the fake tracker is shared by
  every project of the process.
- `start_local()` (the dev server, real time) was considered: it also downloads a binary, and for a
  demonstration it adds nothing the time-skipping server does not give.

## Consequences

`docs/development.md` ("Demonstration") says how to run it. `apps/api/tests/test_demo_inline_worker.py`
proves it: a work item created through the API reaches its human gate by the agent's step, stays there,
and moves to its terminal state within a bounded time once a decision is posted; the mode is off unless
asked, and refused in `staging`, `prod` and without the fakes. The ceilings of ADR 0099 are untouched
(one new lazy crossing, in a module no router imports).
