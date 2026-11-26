# 0226 — A person's own machine runs that person's runs: the personal runner is a personal outpost

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends
  [ADR 0147](0147-an-outpost-only-dials-out.md) §1, §4, §11 and its refused "personal device as an execution
  place of a shared project's workflow", [ADR 0149](0149-a-tool-may-live-at-an-outpost.md) §12 and
  [ADR 0222](0222-an-agent-does-not-ship-unisolated-in-production-without-a-named-opt-out.md) §2)
- **Concerns**: `outposts` (a new `owner_user_id`) and a new table `outpost_runs`
  (`apps/api/src/tranibor_api/db/models/outposts.py`); the routes `/me/runners` (a new `routers/me_runners.py`)
  and the administrator's routes of `routers/outposts.py`; the door's poll (`outposts/door.py`: `_look`,
  `_poll`), the rendezvous (`outposts/rendezvous.py`: `claim`, `_unclaimed`, `sign_call`) and
  `tranibor_adapters.sealing`; the door's contract `packages/contracts/outpost-door.yaml` (protocol 2) and a new
  `packages/contracts/schemas/outpost-run.schema.json`; `ExecutorKind` and `EXECUTOR_CAPABILITIES`
  (`packages/adapters/src/tranibor_adapters/base.py`), `RoutingExecutor`; `prepare_stage` (`activities/stage.py`),
  `start_run` and `await_run` (`activities/execution.py`), a new `activities/placement.py`; the columns
  `runs.executed_at` and `runs.work_owner_user_id`; the policy `sandbox.personal_runner` (`policy.schema.json`);
  a conversation's turn (`services/conversations.py` `_turn`, `services/agent_sessions.py` `respond`,
  `routers/agent_session_pods.py`); the daemon `choregos-outpost` (`packages/outpost`). Builds on
  [ADR 0010](0010-gates-are-mechanisms.md), [ADR 0101](0101-governance-the-platform-does-not-enforce-is-refused.md),
  [ADR 0134](0134-every-person-has-a-personal-space-conversations-are-durable.md),
  [ADR 0143](0143-a-run-that-ended-stays-ended.md), [ADR 0144](0144-the-run-s-api-answers-only-where-runs-are.md),
  [ADR 0146](0146-the-runner-and-the-agent-do-not-share-a-container.md) and
  [ADR 0148](0148-the-outpost-door-is-a-third-door-with-its-own-contract.md). Siblings of the same day: what a
  run on a person's own subscription spends and where that credential lives (ADR 0227), the personal
  installation (ADR 0228), the surfaces that install and start the daemon (ADR 0229), the relay (ADR 0230), a
  responsible person's step at their runner (ADR 0234)

## Context

The product owner wants one person to run Choregos for themself, with the agents on their own machine and their
own vendor sign-in (decisions of 2026-11-26). The agent must therefore run where that sign-in is. What exists:

- **An outpost dials out** (ADR 0147): its instances make their keys on the spot, poll a door for 25 seconds
  (ADR 0148) and claim calls under a lease and an epoch (`rendezvous.claim`). It carries `tools` only. ADR 0147
  §1 keeps it "the organisation's, never a project's or a person's", and ADR 0149 §12 lists what would have to
  hold before `runs`: `OutpostCapability.RUNS` is reserved and `AnnouncedCapability` is `Literal["tools"]`.
- **A run is pushed.** `start_run` mints the run token when the run is queued (`mint_run_token`), and every
  executor starts a pod or a container the platform reaches.
- **A person's own work has a place**: the personal space (ADR 0134), under the restrictive policy
  `projects_personal_owner`.
- **Nothing records where a run executed**, nor whose machine may execute it.

A laptop is not a pod. It sleeps, its owner is root, and the platform verifies nothing on it. Two questions
follow, and this record answers each apart: whose runs may go to a person's machine (that person's own, and no
one else's), and what the platform promises about them (what it refuses at placement, and what it only records).

## Decision

1. **A personal outpost: one per person and organisation, each machine an instance.** `outposts.owner_user_id`
   names the person; a unique index on (`org_id`, `owner_user_id`) keeps one per person, as
   `uq_projects_personal_owner` keeps one personal space. Each of the person's machines is an instance (ADR 0147
   §2 unchanged). A personal outpost announces `runs` and nothing else; an organisation's outpost announces
   `tools` and nothing else, and `OutpostCreate` still refuses `runs` (`apps/api/tests/test_outposts_api.py`).
   Its instances, enrolments and offers are under a restrictive policy copied from `projects_personal_owner`:
   seen by the owner's sessions, by the door that names the outpost, and under the platform scope; by no other
   person. The `outposts` row stays an organisation row. Held by
   `apps/api/tests/test_personal_outposts_postgres.py::test_rls_hides_a_personal_runner_from_another_user` (a raw
   SELECT under another user's scope finds no row) and
   `apps/api/tests/test_personal_outposts.py::test_a_member_never_sees_another_members_runner` (404).
2. **Enrolled and approved by its owner only.** The owner creates the outpost, asks for the enrolment token and
   approves the instance against its fingerprint through `/me/runners` (`listMyRunners`,
   `createMyRunnerEnrolment`, `approveMyRunnerInstance`, `pauseMyRunner`, `resumeMyRunner`, `revokeMyRunner`), in a
   session that carries an authentication time; a token still cannot approve (ADR 0147 §4). The daemon enrols with
   the door's existing signed `enrol` verb. In a personal installation, `choregos local` signs the owner in with an
   owner ticket on the loopback door (ADR 0228) and runs the three steps. A hosted deployment uses the device flow
   later (`routers/device_sign_in.py`, `client_kind: runner`, a single-use session that opens those two routes
   only), with phase C. Every administrator route under `/orgs/{org}/outposts/{slug}/` that reaches an instance,
   an enrolment or a call refuses a personal outpost (403 `personal_outpost`); the routes on the outpost itself
   accept it: list, get, pause, revoke, delete. A pause is lifted by the side that made it: an administrator's by
   `resumeOutpost`, the owner's by `resumeMyRunner`. Why: an administrator who could enrol a machine of theirs into
   a person's outpost would receive that person's sealed runs and run them on the administrator's own subscription
   (ADR 0227). Held by
   `apps/api/tests/test_personal_outposts.py::test_every_admin_route_is_classified_for_a_personal_outpost`, which
   reads the route table of `routers/outposts.py`: a route added later is classified or the test fails.
3. **Door protocol 2: a runs lane inside the poll, and no new verb.** `runs` is announced from protocol 2 only, by
   an instance of a personal outpost. The poll's request gains `runs_running` (the offers the instance runs:
   listing one renews its lease) and `runs_ended` (each with its exit code), and states what the instance takes:
   its modes (`docker`, `native`), the offer kinds it knows, its capacity. The answer gains `runs` (each offer
   sealed and signed) and `cancel_runs`. A protocol-1 daemon ignores both answer fields (`OutpostPollAnswer` is
   lenient) and is never handed an offer. ADR 0148 §3 holds: four verbs, and `DOOR_ROUTES`
   (`tests/licences/test_mcp_gateway_boundary.py`) is unchanged; the lane lives in the door's directory and imports
   no broker module (ADR 0148 §12). Held by
   `packages/contracts/tests/test_outpost_contracts.py::test_runs_is_announced_from_protocol_2_only` (it replaces
   `test_runs_is_reserved_and_no_message_of_protocol_1_carries_it`) and
   `apps/api/tests/test_outpost_door_runs.py::test_protocol_1_never_gets_a_run`.
4. **An offer is claimed once, and its run token is minted at the claim.** An offer is a row of `outpost_runs`
   (the run's id, its outpost, its kind, the mode placement chose, its state: `offered`, `claimed`, `ended`,
   `lost`, `cancelled`, `expired`). `_look` claims offers for the polling instance with `FOR UPDATE SKIP LOCKED`
   and a compare-and-set on the state, as a call is claimed, and only an offer whose kind and mode the instance
   announced, within its capacity. Then, and never before, the door mints the run token (`mint_run_token`, valid
   the run's maximum minutes plus 15), seals it with the run's API address and the assignment
   (`outpost-run.schema.json`) to the claiming instance's key (`sealing.seal`, the run's id as associated data),
   and signs the envelope as it signs a call (`sign_call`, ADR 0148 §13). The row never holds the token; it
   exists in the poll's answer only. A claimed offer is never handed over again, to any instance: the platform
   cannot know whether a sleeping laptop started the agent, the reason a claimed write is never leased again (ADR
   0149 §5). The daemon verifies the door's signature against its pinned keys before it opens anything, journals
   the offer before it starts it and reports its end in the next poll. Held by
   `apps/api/tests/test_outpost_door_runs.py` (`::test_two_instances_one_claim`,
   `::test_the_token_is_only_in_the_sealed_envelope`, `::test_a_claimed_offer_is_never_handed_over_again`),
   `apps/orchestrator/tests/test_start_run_on_outpost.py::test_no_run_token_is_minted_at_start` and
   `packages/outpost/tests/test_daemon_runs.py::test_a_run_is_started_once_even_after_a_restart`.
5. **A lapsed lease loses the run, and ADR 0143 closes it.** Each poll of the claiming instance that lists the
   offer in `runs_running` sets its lease to `lease_s` from that poll, never past the claim plus the run's maximum
   minutes; a poll of another instance renews nothing, and nothing else extends it (ADR 0148 §9 holds: the
   lifetime is the door's, whatever the daemon asks for).
   A lease that lapses (the lid closed, the machine offline) or a revocation marks the offer `lost`. The executor
   reports it as a run it no longer knows, and the orchestrator closes the row with the reason `lost` before
   anything else (ADR 0143 §2): from then on the run's API answers the late process 409 `run_closed` (ADR 0143
   §5), and that instance's next poll carries the run in `cancel_runs`. The workflow's retry makes a new run id.
   Nothing is added to the token and `refuse_closed_run` reads no new row: ADR 0143 §9 holds. Between the lapse
   and the close, one interval of `await_run` at most (`heartbeat_seconds`), the claimed process can still call,
   the same window any executor's lost run has. Held by
   `apps/api/tests/test_outpost_door_runs.py::test_a_poll_that_omits_a_run_lets_its_lease_lapse` and
   `::test_a_lost_offer_closes_the_run_and_its_api_answers_run_closed`.
6. **An executor kind `outpost`, with the capabilities `queue` and `claim`.** `claim` joins
   `EXECUTOR_CAPABILITIES`: the token is minted when an instance claims the run, so `start_run` mints none for
   such an executor. `start` writes the offer and starts nothing; `status` reads it (`offered` is pending, "waiting
   for <owner>'s runner"; `claimed` is running; `ended` follows the exit code; `lost` is lost); `cancel` puts the run
   in `cancel_runs`. An offer nobody claims is withdrawn at the queue bound (`QUEUE_MAX_MINUTES`, or the run's
   `queue_timeout_minutes`) and the run closes `queue_timeout`, as a queued run does today. The executor sits
   behind `RoutingExecutor` beside the platform's; in a personal installation it is the only one (ADR 0228). Held
   by `apps/orchestrator/tests/test_start_run_on_outpost.py` (`::test_queued_with_the_owner_named`,
   `::test_queue_timeout_withdraws_the_offer`) and
   `packages/adapters/tests/test_outpost_run_executor.py::test_status_maps_the_offer`.
7. **Placement comes first, before the model and the money.** `place_run` (`activities/placement.py`) is the first
   step of `prepare_stage`, before `ModelResolver` and `_reservation`, because a run on a person's subscription
   resolves its model and reserves differently (ADR 0227). It is a step of an existing activity, not a new
   activity: nothing under `workflows/` changes. It decides where the run executes, in which mode, and for whom;
   `runs.executed_at` (`platform` or `outpost:<id>`) and `runs.work_owner_user_id` are written beside
   `_record_chain`, and `run.placement` is journaled.
8. **Whose runner: the work owner's, never the initiator's.** A run is offered only to the personal outpost whose
   `owner_user_id` is the run's work owner: in phase A, the owner of the personal space the run belongs to; in
   phase C, the responsible person who accepted the work item (ADR 0233, ADR 0234). Never the initiator, who can be
   an API token, an automation, a rule or a teammate: none of them may drive a person's machine for work that is
   not that person's. A run is never offered to another person's runner, and in phase A a shared project never
   places on a person's runner, whatever its policy says. Held by `apps/orchestrator/tests/test_placement.py`
   (table-driven over personal space and shared project, runner present or absent, modes;
   `::test_never_another_persons_runner`, `::test_a_shared_project_never_places_on_a_runner_in_phase_a`).
9. **The policy `sandbox.personal_runner`**: `{placement: never | preferred | required, prefer_wait_minutes,
   credential, native: refused | accepted}`. `required` waits for the work owner's runner (6) and tells the person;
   a work owner with no runner is refused, not retried. `preferred` takes the runner when it is present
   (`services.outposts.presence`) and can honour the run, otherwise the platform, after `prefer_wait_minutes` at
   most. `never` is the platform. The personal posture (ADR 0228) defaults to `required`; `credential` is ADR
   0227's. In phase A a runner takes subscription runs only: an offer carries no gateway key. A gateway key sealed
   for a laptop that may sleep for hours, with the budget it would hold, is deferred; a person without a
   subscription runs on a platform executor.
10. **Docker first, native by name.** In `docker` mode the daemon starts the run with `LocalDockerExecutor` and
    `separate_agent` (ADR 0146): the runner's container and the agent's, secrets passed by name, on the personal
    installation's `runs` network, which reaches outside only through the egress proxy. `native` is an opt-in of
    the daemon's local policy: the runner as a process and the agent its child (isolation `process`), confined by
    Landlock on Linux (`tranibor_runner.confine`, writes only), unconfined on macOS, refused on Windows, where the
    daemon's journal and the launcher are POSIX-only (WSL2 or docker mode instead). The judgment of ADR 0222 is
    made at placement, for the mode chosen: `place_run` calls `unisolated_agent_refusal` with docker as a separate
    agent and native as `process`, and with the organisation's policy `native: accepted` as the opt-out; a project
    may tighten that policy, never widen it (`tranibor_core.policy_ceiling`). In staging, prod and the personal
    posture a native run is therefore placed only under `native: accepted`, logs `agent_isolation.none_accepted`
    with `outpost:<id>`, and the runner repeats the judgment from its assignment. Placement also refuses what a
    laptop cannot honour: a `sandbox.runtime` of `gvisor` or `kata`, an A2A step
    ([ADR 0062](0062-remote-agents-over-a2a.md)), and a native mode where the deployment holds runs' egress behind
    a proxy ([ADR 0070](0070-network-access-is-bounded-and-enforced-per-run.md)) without `native: accepted`. Under
    `required` such a run fails with a code naming the field; under `preferred` it stays on the platform. Held by
    `apps/orchestrator/tests/test_placement.py` and `packages/adapters/tests/test_personal_isolation.py`
    (`::test_native_refused_in_prod_without_the_policy`, `::test_accepted_and_logged_with_it`).
11. **A conversation turn is an offer of its own, one per turn.** A conversation placed on the personal runner
    (ADR 0227) offers each turn, kind `conversation_turn`, to the outpost owned by the conversation's owner
    (`agent_sessions.user_id`) and to no other. `respond` hands it to `respond_on_runner`; `_turn` keeps
    `claim_turn`, the conversation's lease, cancel and the journal. An offer not claimed within `claim_wait_s` (20
    seconds by default) fails the turn `runner_offline`: nothing is queued across a sleep. The claim mints a run
    token whose subject is `turn-{conversation}-{n}`, valid the turn's maximum minutes plus 5, sealed to the
    claiming instance; it speaks for that turn only, on the internal session routes (`session_pod_record`). A lapsed
    lease fails the turn `runner_lost` and drops the partial answer; a later post is refused 409 (`lease_lost`).
    Between turns the runner keeps no platform credential: its ACP session id, the last turn it recorded and the
    instructions' digest only; the platform's history is the source of truth. Held by
    `apps/api/tests/test_conversation_on_runner.py::test_a_runner_that_sleeps_mid_turn_loses_it`,
    `apps/api/tests/test_conversation_runner_tokens.py::test_a_turn_token_works_for_its_turn_only` and
    `packages/outpost/tests/test_daemon_turns.py`.
12. **Callbacks go to the run door, and the daemon is not a surface.** A run at a personal outpost calls the run's
    API at a run door (ADR 0144, `TRANIBOR_RUN_DOOR_HOSTS`): in a personal installation, its loopback and its `runs`
    network (ADR 0228). Never through the outposts' door, which carries assignments and no run API (ADR 0148 §12),
    and never through the relay (ADR 0230), whose listener answers 404 to `/outposts/*` and `/api/v1/internal/*`.
    A hosted deployment's runners wait for the run door proven on a real ingress (S65-02). The daemon shows
    nothing, signs no person in and decides nothing: it runs what the platform decided, for its owner. It is not a
    surface of ADR 0154 or ADR 0155; a surface may install, start and stop it as a separate process (ADR 0229).

## What is refused, and what is only recorded

The platform verifies nothing on a person's machine: not the wall, not the isolation, not the egress. ADR 0149
§12 asked for a verified wall and a CONNECT proxy pinned to two hostnames because an organisation's outpost runs
inside a network for many people. A personal runner runs on its owner's machine, for its owner alone. So the
platform refuses what it knows that place cannot honour, records the rest, and does not offer the rest as a
guarantee (ADR 0101).

| Concern | Refused | Only recorded |
|:--|:--|:--|
| Who | an administrator's enrolment or approval on a personal outpost (403); an offer to another person's runner; the initiator as work owner; a shared project's run on a runner in phase A | who enrolled and approved each instance (audit `outpost.approve`) |
| Protocol | `runs` from an organisation's outpost or a protocol-1 instance; a second claim; a hand-over after a claim; the run's API after the close (409 `run_closed`); a turn's post after its lease (409 `lease_lost`) | the instance that claimed, when, its daemon version |
| Place | `gvisor` or `kata`; an A2A step; native without `native: accepted` in staging, prod and the personal posture; native on Windows | `executed_at`, the mode, the isolation, the confinement (the Landlock ABI, none on macOS), `egress: not observed` for a native run, `agent_isolation.none_accepted` for each native run |
| Evidence | a gate with `measured_only` (`gates/__init__.py`) refuses an outpost run's facts, which are declared (`packages/core/tests/test_gates_executed_at.py::test_measured_only_refuses_runner_facts_of_an_outpost_run`) | the facts themselves, as the runner declared them |

## Unchanged, on purpose

- **ADR 0143 §9** ("No column, no migration, no claim in the token"): the closure of a lost run adds none of the
  three. `runs.executed_at` and `runs.work_owner_user_id` are this record's columns; no route of the run's API
  reads them.
- **ADR 0148 §3 and §12**: four verbs, `DOOR_ROUTES` frozen, no broker import in the door's directory.
- **ADR 0061 §3**: only a project's agents run in a project's workflow, wherever the run executes.
- **ADR 0147, "a phone as an execution place"**: refused; a phone stays a surface (ADR 0193).
- **ADR 0147, "a local model gateway with a declared cost"**: refused; a runner hosts no model gateway, and what a
  subscription run books is ADR 0227's unmeasured line, not a declared cost.
- What a runner-placed turn changes in ADR 0134 §3, §5 and §6, ADR 0080 §10 and the ADR 0141 table (a credential
  per turn, a title cut from the first message, deltas relayed from the runner) is recorded with the stories that
  build it, not here.

## Alternatives rejected, and when each wins

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| A `runners` object with its own tables, door and daemon | the protocol must diverge from the outposts' | it duplicates enrolment, signed requests, the poll, the journal and the uplink, which then drift | **Refused**: the outpost gains an owner |
| `runs` on an organisation's outpost | a build needs what only that network has | ADR 0149 §12 must hold first (verified wall, pinned proxy) and no scenario asks for it | Not now; ADR 0149 §12 stands for them |
| Verbs of their own (`runs/{id}/heartbeat`, `runs/{id}/exit`) | polls are rare | a poll every 25 seconds already carries lanes (ADR 0148 §7); each verb is one every daemon must learn | **Refused** (ADR 0148 §3) |
| An epoch or the outpost in the run's token | one token serves several claims | it amends ADR 0143 §9 and its refused "token epoch"; claiming once makes one token per run unambiguous | **Refused** |
| A lease read in `refuse_closed_run` on every call of an outpost run | the window between lapse and close must be zero | one more read on every call, ADR 0143's refused deny-list in another form | **Refused**; the close suffices |
| The token minted at queue time and renewed (`renew`) | queues are short | a laptop may be away for hours, and ADR 0149 §12 asks for minting at claim | **Refused** |
| A claimed run handed over again after a lapse | the claim provably never started | the platform cannot know | **Refused**, as ADR 0149 §5 for writes |
| The initiator's runner | the initiator is always the person | it can be a token, an automation or a teammate | **Refused** |
| The run's API through the outposts' door or the relay | there is no run door | a generic tunnel (ADR 0147), and the relay's operator would carry every run's token | **Refused** |
| One long-lived offer per conversation | turns follow each other fast | a credential lives between turns (ADR 0134 §3 is per turn) and a process per open conversation | **Refused**; one offer per turn |
| `TRANIBOR_AGENT_ISOLATION=none-accepted` as the opt-out for native runs | one operator owns every machine | it accepts every member's laptop and the platform's own executors at once | **Refused**: the organisation's policy |

## Consequences

- One contract-change PR carries door protocol 2, `outpost-run.schema.json`, `ExecutorKind.OUTPOST`, the
  capability `claim`, `/me/runners`, `client_kind: runner` and `sandbox.personal_runner`; the migrations add a
  column to `outposts`, two to `runs` and the table `outpost_runs`, each a security path for human review.
- The door mints run tokens. Where it runs as its own Deployment (ADR 0148 §8) it holds the run-token private key,
  a wider blast radius than today; the chart gives it that key only when runs are on.
- A laptop that sleeps loses its runs, and the retry repeats their work. A sleep inhibitor held by the daemon
  while it runs (phase A2) reduces this; it does not remove it.
- A run waiting for an offline runner holds nothing: no token, no key, no reservation.
- Native mode, the gate change for `measured_only` and local write roots come after the first end-to-end run
  (phase A2). Until then a runner runs in docker mode only, and the status rows say so.

## Not proved

This record is the decision; it proves nothing. No run has executed on a real laptop: sleep, Docker Desktop's
networking and macOS are untried. The run door is unproven on a real ingress (S65-02), so a hosted deployment's
runners wait for it. Whether a person's subscription may serve a shared project's request is a reading of the
vendor's terms that phase C waits for (ADR 0234).
