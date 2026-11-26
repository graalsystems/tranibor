# Plan — 2026-11-26: Choregos for oneself, on one's own subscription

> English on purpose (ADR 0046). The plan is committed by its first story, S87-00. A supervising session may
> fan it out one story per worker and one PR per story; `sprint` in the backlog is the plan's wave.

Source: the product owner's requests of 2026-11-26. They asked whether Choregos could use a person's own Claude or
Gemini subscription ("with Claude we have setup tokens"), how a desktop agent environment that runs the vendors'
CLIs on its users' subscriptions does it, and then for three situations to work end to end:

1. **(first)** one person runs Choregos for themself, downloaded as the CLI, a desktop app (to create) or with
   Docker, and uses it from the web console or from their phone;
2. events from Slack, Microsoft Teams or Notion concern a person and call for an action (a workflow or other);
3. an organisation, an association or a team with projects, where each person takes charge of a request and is
   responsible for it end to end.

Throughout, agents run on the person's own machine (a **personal runner**, also used for local actions on that
machine) with the person's **own subscription** (`claude setup-token` gives `CLAUDE_CODE_OAUTH_TOKEN`, valid one
year).

Stream number: **S87**. ADR numbers: **0226** to **0234**, and the reserved **0194**. Before each ADR-creating story,
list `docs/adr/` on `main` and take the next free number if another session took one.

## 0. The constraint that shapes everything

The vendors' terms decide the design, not the other way round. Anthropic's legal and compliance page for Claude
Code says that OAuth authentication of the Free, Pro, Max, Ultra, Team and Enterprise plans supports ordinary use of
Claude Code and Anthropic's own applications, that third-party developers may not offer Claude.ai login in their
applications nor route requests through those credentials on behalf of their users, and that developers may not
collect, store or intermediate Claude.ai credentials or session tokens. It also says an end user may sign in to the
unmodified Claude Code binary with their own subscription. Google's Gemini CLI terms treat third-party tools using
Gemini CLI OAuth as grounds for suspension, and Google enforced it in 2026.

So the platform **never** holds, stores, relays or intermediates a subscription credential, and never uses one for
someone else. The only permitted pattern is the unmodified vendor CLI, signed in by the person, on the person's
machine, for the person's own work. This plan turns that into mechanisms with tests (ADR 0010, ADR 0101): the
credential lives in the personal runner's keychain and reaches only the child environment of the vendor CLI; a
canary proves it is nowhere on the platform side.

The repository was on the wrong side of that line in one place: the demo's `agent-creds` cluster-wide secret, filled
from a person's local credentials file and used by every run (`demo/values-demo.yaml`, `demo/README.md`). S87-56
retires it.

## 1. What exists and what is missing

| Need | State on `main` |
|:--|:--|
| A place that executes a person's runs | **partial**: outposts dial out, enrol with Ed25519/X25519 keys, long-poll the door and claim under a lease (ADR 0147-0149), but carry tools only; `runs` is reserved (ADR 0149 §12) and an outpost is an organisation's, never a person's (ADR 0147 §1) |
| Runs pulled rather than pushed | **missing**: every executor is pushed and the run token is minted at queue time (`activities/execution.py`) |
| The vendor CLI's own login | **partial**: with no run key the backends leave the CLI's authentication alone (`backends/base.py`), but the agent's HOME is a per-run directory and gemini-cli is forced to an API key |
| Costs without a gateway | **partial**: an unmeasured line exists for a gateway without spend read-back and for remote agents (ADR 0062 §6); a budget is still a reservation (ADR 0047) |
| A personal installation | **partial**: `docker compose up` is a development install (development sign-in, fsync off, the Docker socket in the worker) |
| A terminal and an editor | **exists**, but neither may run an agent on the person's machine (ADR 0154 §1, ADR 0155 §1) |
| A desktop app, a relay, Web Push | **missing** (ADR 0194 is cited by ADR 0193 and has no file) |
| Events addressed to a person | **partial**: the person-scoped event door matches an e-mail (`services/task_events.py`); no Slack or Notion intake; Teams only in the agents repository |
| A person responsible for a work item | **missing**: `initiated_by` is fixed at birth and nothing else names a holder |

Everything here is community edition. Nothing reads, needs or changes the enterprise edition.

## 2. Decisions of the product owner (2026-11-26)

| Topic | Decision |
|:--|:--|
| Order | Scenario 1 first, then 2, then 3 |
| Records amended | For this scope only: ADR 0154 §1 and 0155 §1, ADR 0147 (§1 and its refused personal device), ADR 0149 §12, ADR 0107 §6-7, ADR 0004, ADR 0047 and plan decision D6 for subscription runs, ADR 0222's opt-out. ADR 0061 §3 is kept |
| Phone | Through a relay hosted by GraalSystems; the installation dials out |
| Desktop app | Tauri |
| Execution on the laptop | Docker by default; native as an explicit opt-in, recorded on every run |
| Runner offline (scenario 3) | The run waits and its person is told; a project may choose a fallback to the platform |
| Costs of subscription runs | A separate line, not measured, outside every USD budget; turn and minute caps kept |
| Approvals from a chat | A link to the console; the decision is taken after a fresh sign-in |
| Sign-in of a personal installation | No password: an owner key on the computer, a phone paired by QR code, passkeys |
| Console chat | On the subscription from phase A: conversation turns run on the personal runner |
| Phone notifications | Web Push in the browser, with a notification-only service worker |
| Gemini | Claude only in phase A; Gemini or its successor after a spike |

## 3. The architecture, in one page

1. **The personal runner is a personal outpost** (ADR 0226). An outpost gains an owner; each machine is an
   instance; only its owner enrols and approves it. Door protocol 2 adds a runs lane inside the poll, with no new
   verb. An offer is claimed once, the run token is minted at claim and sealed to the instance key, the poll renews
   the lease, and a lapsed lease closes the run through ADR 0143. `executed_at` records where a run executed.
   Placement happens before model resolution and reservation; the run goes to its **work owner** (the personal
   space's owner, later the accepted responsible person), never to the initiator and never to another person.
2. **A subscription stays on its owner's machine** (ADR 0227). The credential source travels in the sealed offer,
   never in `StageInput`. The platform reserves nothing, mints no key and books a `subscription` line. Shapes are
   refused at every platform ingress, the runner scrubs every body it sends by exact value, no platform container
   mounts the Docker socket where subscription runs are allowed, and a canary searches everything.
3. **A personal installation is one person's production** (ADR 0228): `TRANIBOR_ENV=personal`, every production
   refusal, one person enforced by the database, the sole member deciding alone with a fresh sign-in, sign-in by
   owner ticket, QR pairing and passkeys, doors told apart by the edge's listener, a bundle on published images.
4. **Surfaces may supervise** (ADR 0229): `choregos local …` and the Tauri app start the stack and the runner as
   separate processes, and run no agent themselves.
5. **A relay passes TLS through** (ADR 0230): the phone reaches the installation by SNI; the relay never terminates
   TLS and never carries the runner's traffic.
6. **Web Push** (ADR 0194) tells the person a decision waits, straight from the installation to the push service.
7. **Conversations run on the runner too**: one offer per turn, Claude Code with only the platform's tools.
8. **Phase B** adds proven chat accounts and a chat door (ADR 0231) and personal notices (ADR 0232); **phase C**
   adds the responsible person (ADR 0233) and runs their steps at their runner, under their rights (ADR 0234).

## 4. Invariants, each held by a test

1. No subscription credential on the platform side, for runs and conversations (S87-33, S87-40, S87-20, S87-21).
2. No platform service mounts the Docker socket in the personal posture (S87-27).
3. A personal outpost is enrolled and approved by its owner only (S87-15).
4. A subscription serves only its work owner's runs, on the work owner's runner (S87-18, S87-66).
5. The console in the desktop app gets no IPC (S87-48).
6. A hand-over cancels what runs for the previous holder (S87-83).
7. Decisions are taken in a signed-in session with a fresh authentication (unchanged).

## 5. Waves

| Waves | Phase | What lands |
|:--|:--|:--|
| 0 | A | the plan and six records (S87-00 to S87-06) |
| 1 | A | the spike and the contract (S87-07, S87-08) |
| 2 to 6 | A | the posture and its sign-in; the personal outpost, its runs lane, executor and placement; the guards; the runner side; the relay server |
| 7 to 10 | A | the CLI, the bundle, the starter, conversations on the runner, the relay agent, Web Push, the desktop app, the proofs (S87-32, S87-33, S87-40, S87-47) |
| 11 to 16 | B | chat accounts, the chat door, rules, proposals, personal notices |
| 17 to 22 | C | the responsible person and their runner |

Stories of the agents repository carry a `PP-` heading and have no backlog entry here.

## 6. Stories

**Phase A — scenario 1, a person on their own machine.**

### S87-00 · docs(plan): Choregos for oneself on one's own subscription, stream S87 and its backlog stories

Phase A, wave 0, size S. Depends on: nothing.

Acceptance:

- docs/plan/PLAN-2026-11-26-personal-use.md is in the repository and every S87 story heading of the plan is a backlog story
- tests/docs/test_plan_s87_in_backlog.py turns red when a story is removed from the backlog or when a dependency does not come before its story

Where: `docs/plan/PLAN-2026-11-26-personal-use.md`, `docs/plan/status/S87.md`, `tests/docs/test_plan_s87_in_backlog.py`.

The test that fails without it: `tests/docs/test_plan_s87_in_backlog.py`.

PLAN-2026-11-26-personal-use. sprint is the plan's wave. The stories of the agents repository (PP-NN) have no entry here.

### S87-01 · docs(adr): 0226, a person's own machine runs that person's runs (the personal outpost)

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The record is indexed as proposed and quotes each clause it amends: ADR 0147 §1, §11 and its refused personal device, ADR 0149 §12, ADR 0222 §2
- Each amended record carries a dated amendment note pointing back

Where: `docs/adr/0226-a-person-s-own-machine-runs-that-person-s-runs.md`, `docs/adr/README.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

The product owner's decisions of 2026-11-26. ADR 0143 §9, ADR 0148 §3/§12 and ADR 0061 §3 are kept.

### S87-02 · docs(adr): 0227, a person's own subscription is used only by the vendor CLI on that person's runner

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The record is indexed as proposed, starts from the vendor terms and names every mechanism with the test that will hold it
- Plan decision D6 is amended in docs/plan/00-index.md for subscription runs only; ADR 0107 §6-7, 0004, 0047 §1, 0010, 0029 §5, 0134 §3/§5/§6, 0080 §10 and the 0141 table carry amendment notes

Where: `docs/adr/0227-a-person-s-own-subscription-is-used-only-by-the-vendor-cli-on-that-person-s-runner.md`, `docs/plan/00-index.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

A decision PR for D6. ADR 0109 §1 is unchanged: a subscription is not a gateway.

### S87-03 · docs(adr): 0228, a personal installation is one person's production

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The record is indexed as proposed; plan decisions D9 and D11 are amended for the personal posture only; ADR 0184 §1 and ADR 0029 §3 carry amendment notes

Where: `docs/adr/0228-a-personal-installation-is-one-person-s-production.md`, `docs/plan/00-index.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

A decision PR for D9 and D11. The development compose.yaml stays a developer install.

### S87-04 · docs(adr): 0229, surfaces may install and supervise a personal installation and its runner

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The record is indexed as proposed; ADR 0154 §1/§10 and ADR 0155 §1 carry amendment notes; the desktop app is a fifth surface that runs no agent in its own process

Where: `docs/adr/0229-surfaces-may-install-and-supervise-a-personal-installation-and-its-runner.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

ADR 0151 §3 is unchanged: the CLI binary still refuses every server module.

### S87-05 · docs(adr): 0230, a hosted relay reaches a personal installation by TLS passthrough

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The record is indexed as proposed and says what the relay sees, keeps and never carries (runner and run-API traffic)

Where: `docs/adr/0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

Amends nothing; records why it is not ADR 0147's refused generic relay.

### S87-06 · docs(adr): 0194, a person's devices are told by Web Push, with a notification-only service worker

Phase A, wave 0, size S. Depends on: S87-00.

Acceptance:

- The reserved number is written and indexed as proposed; ADR 0193 carries an amendment note (a progressive web app is refused except for notifications)

Where: `docs/adr/0194-a-person-s-devices-are-told-by-web-push.md`, `docs/adr/0193-the-phone-is-a-surface-of-the-platform.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

ADR 0193 and S81-02/S81-03 cite 0194, which had no file.

### S87-07 · spike(runner): Claude Code on a subscription through claude-agent-acp, measured

Phase A, wave 1, size M. Depends on: S87-02.

Acceptance:

- Measured in the runner image and natively (Linux, macOS arm64) with HOME forced as workspace.agent_home does: a setup-token token completes an ACP prompt; the ACP error when signed out; usage-limit texts; hosts contacted; loadSession and session/load; CLAUDE_CONFIG_DIR isolation; precedence against ANTHROPIC_API_KEY; request_permission for MCP tools; usage reported
- The findings are fixtures under packages/runner/tests/fixtures/subscription/ that later tests read

Where: `trials/2026-12-personal-subscription/README.md`, `packages/runner/tests/fixtures/subscription/`.

The test that fails without it: the tests of S87-22 and S87-23 fail while the fixtures are missing.

Gates S87-21 to S87-24 and S87-38. A trial README records what was run and on which versions.

### S87-08 · contract(schemas): personal runners, subscription runs, personal sign-in and turns on a runner

Phase A, wave 1, size L. Depends on: S87-01, S87-02, S87-03, S87-04, S87-05, S87-06.

Acceptance:

- contract-change, additions only: door protocol 2 (runs lane, cancel_runs, runs_running, runs_ended, runner block), outpost-run.schema.json, ExecutorKind outpost, executor capability claim, /me/runners, client_kind browser and runner, /auth/owner, /auth/methods, /me/device-pairings, /auth/device/session, passkey routes, cost kind subscription, policy sandbox.personal_runner, run executed_at and credential, Conversation.placement, AgentSessionTurn and its result extended, the turn deltas route, offer kind conversation_turn, relay.yaml, local-status.schema.json
- OutpostCreate still refuses runs; test_runs_is_reserved_and_no_message_of_protocol_1_carries_it becomes test_runs_is_announced_from_protocol_2_only

Where: `packages/contracts/outpost-door.yaml`, `packages/contracts/openapi.yaml`, `packages/contracts/schemas/outpost-run.schema.json`, `packages/contracts/src/tranibor_contracts/enums.py`, `packages/contracts/relay.yaml`.

The test that fails without it: `packages/contracts/tests/test_outpost_contracts.py::test_runs_is_announced_from_protocol_2_only`.

One contract-change PR (ADR 0001), human review owed (ADR 0180 §3). No StageInput change.

### S87-09 · feat(core): personal is a production environment, and production is named in one place

Phase A, wave 2, size M. Depends on: S87-03.

Acceptance:

- TRANIBOR_ENV=personal is production: dev login, fakes, demo connectors, the direct gateway, a superuser role, clear Temporal payloads and unisolated agents are refused
- Every literal staging/prod check calls is_production(); an architecture test refuses a new one

Where: `packages/core/src/tranibor_core/production.py`, `apps/api/src/tranibor_api/config.py`, `packages/adapters/src/tranibor_adapters/builtins.py`.

The test that fails without it: `tests/architecture/test_production_is_named_once.py`.

packages/core/src/tranibor_core/production.py is the one place.

### S87-10 · feat(api): a personal installation holds one person, enforced by the database

Phase A, wave 3, size M. Depends on: S87-09.

Acceptance:

- An installation row records the posture; a trigger on memberships refuses a second person on every path; machine accounts are accepted; the API refuses to start on a mismatch

Where: `apps/api/src/tranibor_api/installation.py`, `apps/api/src/tranibor_api/bootstrap.py`.

The test that fails without it: `apps/api/tests/test_personal_single_member_postgres.py`.

Proven on PostgreSQL under the application role.

### S87-11 · feat(api): the sole member decides alone, with a fresh sign-in, recorded as such

Phase A, wave 4, size M. Depends on: S87-10.

Acceptance:

- In the personal posture the only person passes separation of duties, group-addressed steps and ADR 0196 excluded, always with a fresh authentication, and the decision records sole_member: true; the same case stays refused in prod

Where: `apps/api/src/tranibor_api/services/actions.py`, `apps/api/src/tranibor_api/services/human_assignee.py`.

The test that fails without it: `apps/api/tests/test_sole_member_decides.py`.

services/actions.py, services/agent_releases.py, services/human_assignee.py.

### S87-12 · feat(api,web): the owner signs in on the loopback door; the edge names the door by listener

Phase A, wave 3, size L. Depends on: S87-09.

Acceptance:

- POST /auth/owner opens a session from a 60-second single-use ticket signed by the owner key, on the loopback door only; per-door cookies; a request without a door in the personal posture is refused

Where: `apps/api/src/tranibor_api/owner_sign_in.py`, `apps/api/src/tranibor_api/routers/auth.py`, `apps/web/src/app/login/page.tsx`.

The test that fails without it: `apps/api/tests/test_owner_sign_in.py`.

The door is decided by the edge listener, never by the Host header.

### S87-13 · feat(api,web): pair a phone from the computer with a one-time link shown as a QR code

Phase A, wave 4, size M. Depends on: S87-12.

Acceptance:

- Only the owner on the loopback door, freshly signed in, creates a 5-minute pairing; it is exchanged once on the relay door for a session listed and revocable under My account

Where: `apps/api/src/tranibor_api/routers/device_sign_in.py`, `apps/web/src/app/pair/page.tsx`.

The test that fails without it: `apps/api/tests/test_device_pairing.py`.

client_kind browser; a computer-initiated pre-approved device authorization.

### S87-14 · feat(api,web): passkeys sign the owner in and re-authenticate them on the relay door

Phase A, wave 5, size L. Depends on: S87-13.

Acceptance:

- WebAuthn with the relay hostname as RP ID and user verification; a passkey re-authentication counts as fresh for ADR 0170; a regressed counter and a foreign origin are refused

Where: `apps/api/src/tranibor_api/passkeys.py`, `apps/web/src/app/account/passkeys/page.tsx`.

The test that fails without it: `apps/api/tests/test_passkeys.py`.

Every route answers 404 outside the personal posture.

### S87-15 · feat(outposts): a person's own outpost, enrolled and approved by its owner only

Phase A, wave 2, size M. Depends on: S87-08.

Acceptance:

- outposts.owner_user_id with a restrictive RLS policy; /me/runners; one per person and organisation; every administrator route refuses to enrol or approve a personal outpost

Where: `apps/api/src/tranibor_api/db/models/outposts.py`, `apps/api/src/tranibor_api/routers/me_runners.py`.

The test that fails without it: `apps/api/tests/test_personal_outposts.py`.

Organisation outposts stay tools-only.

### S87-16 · feat(outposts): the door's runs lane, an offer claimed once, the run token minted at claim

Phase A, wave 3, size L. Depends on: S87-15.

Acceptance:

- outpost_runs claimed once (SKIP LOCKED + CAS); the token is minted at claim and sealed to the instance key; a poll naming the run renews its lease; a lapsed lease marks it lost and the run is closed through ADR 0143

Where: `apps/api/src/tranibor_api/outposts/runs.py`, `apps/api/src/tranibor_api/outposts/door.py`.

The test that fails without it: `apps/api/tests/test_outpost_door_runs.py`.

Reuses outposts/door.py, outposts/rendezvous.py and sealing.py.

### S87-17 · feat(executor): the outpost executor offers a run and mints nothing

Phase A, wave 4, size L. Depends on: S87-16.

Acceptance:

- ExecutorKind outpost (queue, claim, separate_agent): start writes the offer, status reads it (waiting for the owner's runner), cancel goes through cancel_runs, the queue timeout withdraws it

Where: `packages/adapters/src/tranibor_adapters/executor/outpost_runs.py`, `apps/orchestrator/src/tranibor_orchestrator/activities/execution.py`.

The test that fails without it: `apps/orchestrator/tests/test_start_run_on_outpost.py`.

Registered as a runtime type behind RoutingExecutor.

### S87-18 · feat(orchestrator): a run is placed on its work owner's runner, or refused, never downgraded

Phase A, wave 5, size M. Depends on: S87-17.

Acceptance:

- place_run runs in prepare_stage before model resolution and reservation; work_owner is the personal space's owner, never the initiator; the personal posture defaults to required and subscription; a subscription profile maps to the provider's model id
- runs.work_owner_user_id and on_behalf_of are recorded next to the run chain

Where: `apps/orchestrator/src/tranibor_orchestrator/activities/placement.py`, `apps/orchestrator/src/tranibor_orchestrator/activities/stage.py`.

The test that fails without it: `apps/orchestrator/tests/test_placement.py`.

Refused at placement: gVisor, A2A steps, policies a laptop cannot honour.

### S87-19 · feat(cost): a subscription run reserves nothing, mints no key, and is booked apart

Phase A, wave 6, size M. Depends on: S87-18.

Acceptance:

- No reservation, no key, no spend read; one ledger line of kind subscription at 0, unmeasured, outside every USD budget; turns and minutes still end the run

Where: `apps/orchestrator/src/tranibor_orchestrator/activities/outcome.py`, `apps/orchestrator/src/tranibor_orchestrator/activities/run_key.py`.

The test that fails without it: `apps/orchestrator/tests/test_subscription_spend.py`.

The run keeps a nominal budget.usd the contract requires, labelled not a budget.

### S87-20 · feat(security): a subscription credential is refused and scrubbed on the platform side

Phase A, wave 2, size L. Depends on: S87-08.

Acceptance:

- One module defines the subscription shapes; they join SECRET_PATTERNS, redaction and SECRET_FILE_PATTERNS; /api/v1/internal refuses them; connector, secret and launch-env writes answer 422; DirectGateway and the worker refuse a shaped value

Where: `packages/core/src/tranibor_core/subscription_credentials.py`, `packages/core/src/tranibor_core/redaction.py`.

The test that fails without it: `packages/core/tests/test_subscription_credentials.py`.

No Temporal scrub codec (ADR 0227).

### S87-21 · feat(runner): the personal-runner mode, subscription only at an outpost, scrubbed by exact value

Phase A, wave 3, size M. Depends on: S87-07, S87-08.

Acceptance:

- A subscription run off an outpost exits with its own code; no model listener; gateway variables removed; every URL and body the runner sends the platform is scrubbed by exact value; a push whose diff holds the token is refused

Where: `packages/runner/src/tranibor_runner/runner.py`, `packages/runner/src/tranibor_runner/loopback/agent.py`.

The test that fails without it: `packages/runner/tests/test_subscription_run.py`.

The runner's single HTTP client to the platform carries the scrub.

### S87-22 · feat(runner,outpost): Claude Code on the person's subscription

Phase A, wave 4, size M. Depends on: S87-21.

Acceptance:

- choregos-outpost personal login claude reads the token from stdin into the OS keychain or a 0600 file; the daemon announces anthropic only while one is present; the token reaches only the child environment of a subscription run; the ACP pre-flight maps a signed-out CLI to subscription_login_required

Where: `packages/outpost/src/tranibor_outpost/secrets.py`, `packages/runner/src/tranibor_runner/backends/claude_code.py`.

The test that fails without it: `packages/runner/tests/test_claude_subscription.py`.

Reads the S87-07 fixtures.

### S87-23 · feat(runner,orchestrator): a subscription signed out or at its limit stops for its person

Phase A, wave 5, size M. Depends on: S87-22.

Acceptance:

- Signatures from the spike give subscription_login_required and subscription_limit for subscription runs only; a patch marker makes them needs-human and never retried; older histories replay unchanged

Where: `packages/runner/src/tranibor_runner/budget.py`, `apps/orchestrator/src/tranibor_orchestrator/workflows/interpreter.py`.

The test that fails without it: `apps/orchestrator/tests/test_subscription_never_retries.py`.

Like BUDGET_NEVER_RETRIES.

### S87-24 · feat(network): a subscription run's egress names its provider's hosts, and only that run's

Phase A, wave 6, size S. Depends on: S87-18.

Acceptance:

- Provider hosts measured by the spike are implied only for subscription runs; a deny list wins and placement then refuses the subscription; gateway runs never get a provider host

Where: `apps/api/src/tranibor_api/services/network.py`.

The test that fails without it: `apps/api/tests/test_network_subscription_hosts.py`.

services/network.py.

### S87-25 · feat(outpost): the daemon runs runs in docker mode, never one run twice

Phase A, wave 5, size L. Depends on: S87-16, S87-22.

Acceptance:

- The daemon verifies, opens and journals each run before starting it; docker mode uses LocalDockerExecutor with separate_agent on the runs network, secrets passed by name; cancel stops it; the end is reported in the next poll; capabilities are computed

Where: `packages/outpost/src/tranibor_outpost/runs.py`, `packages/outpost/src/tranibor_outpost/runhost_docker.py`.

The test that fails without it: `packages/outpost/tests/test_daemon_runs.py`.

Native mode is S87-51.

### S87-26 · build(outpost): a native choregos-outpost binary for Linux and macOS

Phase A, wave 6, size M. Depends on: S87-25.

Acceptance:

- A second build target, daemon plus runner, for linux x86_64/arm64 and macOS arm64/x86_64, refusing the API, the orchestrator, sqlalchemy and temporalio; the smoke runs doctor --json; Windows is pointed to WSL2

Where: `tools/cli_binary/build.py`.

The test that fails without it: `tests/tools/test_runner_binary.py`.

Unsigned until BLOCKERS S62-07 is resolved.

### S87-27 · feat(deploy): the personal bundle on published images, with an edge, durable data and no Docker socket in the platform

Phase A, wave 6, size L. Depends on: S87-09, S87-12, S87-16.

Acceptance:

- deploy/personal/compose.yaml uses only published images; only the edge publishes, on 127.0.0.1; no platform service mounts the Docker socket; PostgreSQL keeps fsync; secrets come from a generated file; /outposts and /api/v1/internal are routed on the loopback listener only; outposts on, executor outpost, run-door hosts set
- A runner profile mounts the socket into the person's runner container only

Where: `deploy/personal/compose.yaml`, `deploy/personal/Caddyfile`.

The test that fails without it: `tests/deploy/test_personal_compose.py`.

The development compose.yaml is unchanged.

### S87-28 · feat(cli): choregos local init, up, down, status, doctor, open and logs, by subprocess only

Phase A, wave 7, size L. Depends on: S87-27.

Acceptance:

- init writes the data directory, secrets and the owner key; up starts the bundle of the CLI's own version; open signs an owner ticket; status and doctor answer local-status.schema.json; no server module is imported

Where: `packages/cli/src/tranibor_cli/local/__init__.py`.

The test that fails without it: `tests/architecture/test_cli_local_imports_no_server.py`.

FORBIDDEN_MODULES unchanged.

### S87-29 · feat(cli): the personal runner beside the stack, started, enrolled and watched

Phase A, wave 8, size M. Depends on: S87-28, S87-26, S87-22.

Acceptance:

- local up downloads and verifies choregos-outpost at the stack's version, writes its policy, starts it and enrols it with an owner ticket; local runner login claude passes stdin through without reading it or putting it in argv or environment

Where: `packages/cli/src/tranibor_cli/local/runner.py`.

The test that fails without it: `packages/cli/tests/test_local_runner.py`.

One enrolment API for the CLI and the desktop app.

### S87-30 · feat(deploy,cli): backups and restore of a personal installation, with a proof

Phase A, wave 8, size M. Depends on: S87-28.

Acceptance:

- Nightly and pre-upgrade backups of every database and the object store, a manifest first and a completion marker last; local backup and restore; doctor warns on an old or same-disk backup; a nightly proof restores a finished work item

Where: `deploy/personal/backup.py`, `packages/cli/src/tranibor_cli/local/backup.py`.

The test that fails without it: `tests/deploy/test_personal_backup.py`.

### S87-31 · feat(api): a personal installation seeds its starter into the owner's personal space

Phase A, wave 8, size M. Depends on: S87-27.

Acceptance:

- The offline lock carries the personal-assistant plugin (PP-01); the seed installs it idempotently into the personal space with placement required and credential subscription; the agent stages keep the claude-code backend

Where: `apps/api/src/tranibor_api/personal_seed.py`, `plugins/offline/plugins.lock`.

The test that fails without it: `apps/api/tests/test_personal_seed.py`.

The plugin itself is PP-01 in the agents repository.

### PP-01 · feat(agents): the personal-assistant plugin, for a fresh personal installation

In the agents repository. The plugin requires `requests` and reuses its brief writer and assistant. Its template
`personal-request` runs brief, approval, deliverable and acceptance with no server, so it runs on a personal
installation; its agent stages keep the `claude-code` backend. Evals and structure tests in that repository.
S87-31 locks the published archive.

### S87-32 · ci: the personal installation end to end on Linux, to a finished work item

Phase A, wave 9, size M. Depends on: S87-29, S87-11, S87-19, S87-31.

Acceptance:

- Job personal-install: local init and up on images built in CI, an owner ticket, a work item placed on the runner, a scripted ACP agent on a fake subscription, a sole-member approval, the item done; nightly on the published release

Where: `tools/personal_install_check.py`, `.github/workflows/ci.yml`.

The test that fails without it: the personal-install job.

### S87-33 · test(e2e): the subscription canary, the token is nowhere on the platform side

Phase A, wave 9, size M. Depends on: S87-25, S87-21, S87-20, S87-19.

Acceptance:

- A canary token goes through a full run; it and its base64 and URL forms are absent from every table, decoded Temporal history, docker inspect of every platform container, log, object and relay log; a tool call, a question and a push are part of the run; a positive control sees the agent received it

Where: `tests/e2e/test_subscription_never_reaches_platform.py`.

The test that fails without it: `tests/e2e/test_subscription_never_reaches_platform.py`.

Runs per PR in process and nightly against the bundle.

### S87-34 · feat(api): a conversation records where it runs; on the personal runner it asks no gateway

Phase A, wave 6, size S. Depends on: S87-08, S87-18.

Acceptance:

- agent_sessions.placement, personal_runner by default in the personal posture; opening one calls no gateway; hosted, voice and JSON assistants are refused with the field named; NULL reads as platform

Where: `apps/api/src/tranibor_api/services/conversations.py`, `apps/api/src/tranibor_api/services/agent_sessions.py`.

The test that fails without it: `apps/api/tests/test_conversation_on_runner.py::test_opening_on_the_personal_runner_asks_no_gateway`.

cost_ledger.subscription added by the same migration.

### S87-35 · feat(api): a turn token speaks for its turn only on the internal session routes

Phase A, wave 7, size S. Depends on: S87-34.

Acceptance:

- A turn-{id}-{n} run token is accepted only for a runner-placed conversation, its current turn, under its lease and before the turn is answered; 409 lease_lost otherwise

Where: `apps/api/src/tranibor_api/routers/agent_session_pods.py`, `packages/core/src/tranibor_core/hosted.py`.

The test that fails without it: `apps/api/tests/test_conversation_runner_tokens.py`.

Hosted pods' session tokens are unchanged.

### S87-36 · refactor(api): one toolset for a conversation turn, served to the prompt loop and to the internal tool routes

Phase A, wave 8, size M. Depends on: S87-35.

Acceptance:

- turn_toolset serves the same names to respond and to /internal/agent-sessions/{id}/tools; launch__x only proposes; behaviour of platform conversations unchanged

Where: `apps/api/src/tranibor_api/services/conversation_toolset.py`.

The test that fails without it: `apps/api/tests/test_conversation_runner_tools.py`.

### S87-37 · feat(api): a runner-placed turn is offered to its owner's runner, streamed from its deltas, booked without a gateway

Phase A, wave 9, size L. Depends on: S87-36, S87-16.

Acceptance:

- One conversation_turn offer per turn; deltas journaled and pruned at the answer; the result route books the history once; a subscription ledger line at 0; no key, reservation or model title; an unclaimed offer fails runner_offline, a lapsed lease runner_lost

Where: `apps/api/src/tranibor_api/services/runner_turns.py`, `apps/api/src/tranibor_api/services/turn_follow.py`.

The test that fails without it: `apps/api/tests/test_conversation_on_runner.py`.

### S87-38 · feat(runner): serve-turn, one conversation turn run by Claude Code on the subscription

Phase A, wave 8, size L. Depends on: S87-35, S87-22.

Acceptance:

- A runner-owned CLAUDE_CONFIG_DIR, the instructions as project memory, only the MCP loopback, built-in tools denied twice, ANTHROPIC_* removed; session/load when the platform's history matches, a recap otherwise; deltas batched and scrubbed

Where: `packages/runner/src/tranibor_runner/serve_turn.py`.

The test that fails without it: `packages/runner/tests/test_serve_turn.py`.

### S87-39 · feat(outpost): the daemon hands a conversation_turn offer to serve-turn and stops it on cancel

Phase A, wave 9, size M. Depends on: S87-38, S87-25.

Acceptance:

- The turn token travels in the environment, never argv; the offer stays in runs_running while the child lives; cancel sends SIGTERM; capacity is reported; an unknown kind is refused

Where: `packages/outpost/src/tranibor_outpost/daemon.py`.

The test that fails without it: `packages/outpost/tests/test_daemon_turns.py`.

### S87-40 · test(subscription): the canary covers a conversation turn end to end

Phase A, wave 10, size M. Depends on: S87-37, S87-39, S87-33.

Acceptance:

- A fake agent leaks the token in a chunk, an MCP argument, a tool title and stderr; after the turn the canary is absent from every body sent, table, journal event and log

Where: `tests/subscription/test_conversation_canary.py`.

The test that fails without it: `tests/subscription/test_conversation_canary.py`.

### S87-41 · feat(web): a runner-placed conversation says where it runs and why a turn failed

Phase A, wave 10, size S. Depends on: S87-37.

Acceptance:

- Runs on your runner, on your Claude subscription; no dollar figure; its own message for runner_offline, runner_lost, runner_unsupported, subscription_limited and subscription_unauthorized

Where: `apps/web/src/components/assistants/conversation-view.tsx`.

The test that fails without it: `apps/web/tests/assistants-conversation.test.tsx`.

### S87-42 · feat(relay): the SNI relay routes TLS without terminating it

Phase A, wave 2, size L. Depends on: S87-05, S87-08.

Acceptance:

- A bounded ClientHello parser routes <id>.<domain> to one install's control session and splices bytes; the relay holds no private key for the name; rate limits, suspension, random ids, pairing by device code, metadata-only logs

Where: `packages/sni-relay/src/tranibor_sni_relay/passthrough.py`.

The test that fails without it: `packages/sni-relay/tests/test_passthrough.py`.

packages/sni-relay, Apache-2.0.

### S87-43 · feat(relay,deploy,cli): the installation's relay agent, its own certificate, and choregos local relay

Phase A, wave 8, size L. Depends on: S87-42, S87-27, S87-28.

Acceptance:

- local relay enable pairs the install and sets its public URL; the agent dials out and splices to the edge; the certificate comes by TLS-ALPN-01; /api/v1/internal and /outposts answer 404 on the relay listener

Where: `packages/sni-relay/src/tranibor_sni_relay/agent.py`, `packages/cli/src/tranibor_cli/local/relay.py`.

The test that fails without it: `tests/relay/test_relay_end_to_end.py`.

Proven with the Pebble test CA.

### S87-44 · ops(relay): a relay anyone can run, and the runbook of the hosted one

Phase A, wave 3, size M. Depends on: S87-42.

Acceptance:

- charts/choregos-relay renders with no secret in values and a passthrough service; docs/relay.md says what the relay sees and keeps; the runbook covers abuse, suspension, key rotation, certificate limits and incidents

Where: `charts/choregos-relay/Chart.yaml`, `docs/relay.md`.

The test that fails without it: `tests/charts/test_relay_chart.py`.

The hosted deployment lives in an operations repository, neither the core nor the enterprise edition.

### S87-45 · feat(api,web): Web Push from an installation to its person's browsers

Phase A, wave 8, size M. Depends on: S87-06, S87-28.

Acceptance:

- /me/push-subscriptions and the VAPID key route; keys generated per installation; a pending decision sends an RFC 8291 push straight to the push service, never through the relay; the payload holds kind, title, line and reference; a notification-only service worker and a manifest

Where: `apps/web/public/sw.js`, `apps/api/src/tranibor_api/routers/push_subscriptions.py`.

The test that fails without it: `apps/api/tests/test_push_subscriptions.py`.

Delivers S81-02 and S81-03 for every installation.

### S87-46 · feat(web): the console on a phone through the relay: pairing, passkey, inbox, approval

Phase A, wave 6, size M. Depends on: S87-13, S87-14.

Acceptance:

- On a 390x844 touch viewport: pair, create a passkey, see a pending decision, approve with a passkey step-up, see the item done; no horizontal overflow, 44 px targets

Where: `apps/web/src/app/pair/page.tsx`.

The test that fails without it: `apps/web/e2e/phone.spec.ts`.

### S87-47 · test(e2e): scenario 1 end to end through the relay, from a phone viewport

Phase A, wave 10, size M. Depends on: S87-43, S87-46, S87-32, S87-33.

Acceptance:

- Through the relay with the Pebble CA: a request created from a phone viewport, run by the docker daemon with a fake ACP agent on a canary subscription, its deliverable approved with a passkey, the canary absent everywhere on the platform side

Where: `tests/relay/test_scenario_one.py`.

The test that fails without it: `tests/relay/test_scenario_one.py`.

The single proof of scenario 1.

### S87-48 · feat(desktop): the Tauri app supervises the personal installation and its runner

Phase A, wave 9, size L. Depends on: S87-28, S87-29, S87-43.

Acceptance:

- Sidecars choregos and choregos-outpost; first launch runs doctor, init and up and opens the console signed in; a tray; a native window with bundled assets for Connect Claude; the console origins get no IPC

Where: `apps/desktop/src-tauri/src/main.rs`, `apps/desktop/src-tauri/capabilities/default.json`.

The test that fails without it: `apps/desktop/src-tauri/tests/supervisor.rs`.

apps/desktop.

### S87-49 · test(desktop): the desktop app end to end in CI

Phase A, wave 10, size M. Depends on: S87-48.

Acceptance:

- Linux with a real Docker: first launch to a signed-in console and a work item; Windows: the UI with a fake CLI; macOS: build and Rust tests, said in the status row

Where: `apps/desktop/e2e/first-launch.spec.ts`.

The test that fails without it: `apps/desktop/e2e/first-launch.spec.ts`.

### S87-50 · build(desktop): signed installers and auto-update

Phase A, wave 10, size M. Depends on: S87-48.

Acceptance:

- macOS notarized dmg, Windows Authenticode installer, Linux AppImage and deb; an updater manifest signed with its own key; a backup before a stack upgrade; no downgrade past a migrated schema

Where: `.github/workflows/release.yml`, `apps/desktop/src-tauri/tauri.conf.json`.

The test that fails without it: `apps/desktop/src-tauri/tests/updater.rs`.

Blocked by BLOCKERS S62-07 (certificates).

### S87-51 · feat(gates,isolation): a personal runner's facts are declared; native mode needs a named acceptance

Phase A, wave 7, size M. Depends on: S87-18, S87-25.

Acceptance:

- GateContext carries executed_at and measured_only treats an outpost run's facts as declared; native mode on a personal runner is accepted only through policy native: accepted, logged and journaled per run

Where: `packages/core/src/tranibor_core/gates/__init__.py`, `packages/outpost/src/tranibor_outpost/runhost_native.py`.

The test that fails without it: `packages/core/tests/test_gates_executed_at.py`.

The daemon's native run host comes with it.

### S87-52 · feat(runner,outpost): local actions, the folders a native run may write are named on the machine

Phase A, wave 8, size M. Depends on: S87-51.

Acceptance:

- runs.write_roots in the machine's policy reach Landlock on Linux and are recorded as not enforced on macOS; HOME itself is refused; docker mode needs declared mounts; the platform cannot widen them

Where: `packages/runner/src/tranibor_runner/confine.py`, `packages/outpost/src/tranibor_outpost/policy.py`.

The test that fails without it: `packages/runner/tests/test_local_write_roots.py`.

### S87-53 · feat(outpost): the runner keeps the machine awake while it holds a run

Phase A, wave 6, size S. Depends on: S87-25.

Acceptance:

- caffeinate on macOS, systemd-inhibit on Linux when available, none recorded otherwise; released when the last run ends

Where: `packages/outpost/src/tranibor_outpost/awake.py`.

The test that fails without it: `packages/outpost/tests/test_awake.py`.

### S87-54 · feat(web): My runner, and where a run executed

Phase A, wave 7, size M. Depends on: S87-15, S87-18, S87-19.

Acceptance:

- My runner lists instances, modes, providers announced (never a value), last seen and waiting runs; the run page shows executed_at and the credential; the administrators' page shows personal runners apart with no approve action

Where: `apps/web/src/app/me/runners/page.tsx`.

The test that fails without it: `apps/web/tests/my-runners.test.tsx`.

### S87-55 · feat(api): a person is told when runs wait for their offline runner

Phase A, wave 5, size S. Depends on: S87-17.

Acceptance:

- One notice per runner per hour names the runs that wait and the time left before their queue timeout

Where: `apps/api/src/tranibor_api/services/outpost_runs.py`.

The test that fails without it: `apps/api/tests/test_offline_runner_notice.py`.

Carried by Web Push once S87-45 lands.

### S87-56 · chore(demo): retire the cluster-wide subscription secret and keep it out

Phase A, wave 3, size S. Depends on: S87-20.

Acceptance:

- The agent-creds secret and the copy of a local credentials file leave demo/; a test refuses CLAUDE_CODE_OAUTH_TOKEN, agent-creds and claudeAiOauth in charts, demo, compose files and examples

Where: `demo/README.md`, `demo/values-demo.yaml`.

The test that fails without it: `tests/dev/test_no_platform_held_subscription.py`.

The demo points to the personal runner.

**Phase B — scenario 2, events addressed to a person.**

### S87-57 · docs(adr): 0231, a member proves their chat accounts, and an event is addressed to one person

Phase B, wave 11, size S. Depends on: S87-00.

Acceptance:

- Indexed as proposed; amendment notes on ADR 0161, 0102, 0137, 0097 and 0077; a third party's event may only notify or propose

Where: `docs/adr/0231-a-member-proves-their-chat-accounts-and-an-event-is-addressed-to-one-person.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

Decisions stay in signed-in surfaces (ADR 0102 §2 and 0030 §5 kept).

### S87-58 · docs(adr): 0232, a person is told in person, by a link

Phase B, wave 11, size S. Depends on: S87-00.

Acceptance:

- Indexed as proposed; amendment note on ADR 0153's limits

Where: `docs/adr/0232-a-person-is-told-in-person-by-a-link.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

### S87-59 · fix(api): background task runs are started by subscriber:<email>, not by the person

Phase B, wave 11, size M. Depends on: S87-57.

Acceptance:

- Schedule, poll and event task runs carry subscriber:<email> and act for the person only under a valid ADR 0137 delegation; run_now keeps person:<email>

Where: `apps/api/src/tranibor_api/services/initiator.py`, `apps/api/src/tranibor_api/services/assistant_tasks.py`.

The test that fails without it: `apps/api/tests/test_knowledge_on_behalf.py`.

Flips the assertion at apps/api/tests/test_assistant_tasks.py:89; a release note.

### S87-60 · feat(api): a member proves their Slack account by signing in with Slack

Phase B, wave 12, size L. Depends on: S87-57.

Acceptance:

- A chat_account connector of type slack-oidc enables the link; the team must be allowed; a chat account is never declared by an administrator; no token is kept

Where: `packages/adapters/src/tranibor_adapters/chat_accounts.py`.

The test that fails without it: `apps/api/tests/test_chat_linked_accounts.py`.

Extends services/linked_accounts.py.

### S87-61 · feat(api): Entra and Notion accounts are linked the same way

Phase B, wave 13, size M. Depends on: S87-60.

Acceptance:

- Entra by OIDC and PKCE keyed by oid within its tenant; Notion by OAuth with owner=user, the token dropped

Where: `packages/adapters/src/tranibor_adapters/chat_accounts.py`.

The test that fails without it: `apps/api/tests/test_chat_linked_accounts.py`.

### S87-62 · contract(schemas): ChatEvent v1; feat(adapters): Slack deliveries verified and normalised

Phase B, wave 12, size M. Depends on: S87-57.

Acceptance:

- contract-change: chat-event.schema.json; Slack v0 signing with a 300-second window, url_verification, mentions, direct messages and a shortcut normalised; the model carries no token

Where: `packages/contracts/schemas/chat-event.schema.json`, `packages/adapters/src/tranibor_adapters/chat/slack.py`.

The test that fails without it: `packages/adapters/tests/test_chat_slack.py`.

### S87-63 · feat(adapters): Teams activities and Notion webhooks verified and normalised

Phase B, wave 13, size M. Depends on: S87-62.

Acceptance:

- The Bot Framework token ported from the desk plugin; the Notion signature under its verification token

Where: `packages/adapters/src/tranibor_adapters/chat/teams.py`, `packages/adapters/src/tranibor_adapters/chat/notion.py`.

The test that fails without it: `packages/adapters/tests/test_chat_teams.py`.

### S87-64 · feat(api): the chat door addresses each event to one member through a proven account

Phase B, wave 13, size L. Depends on: S87-60, S87-62.

Acceptance:

- POST /webhooks/chat/{org}/{connector} verifies, answers within three seconds, deduplicates, resolves addressees through verified links only and journals outcomes, never the text

Where: `apps/api/src/tranibor_api/routers/chat_events.py`, `apps/api/src/tranibor_api/services/chat_intake.py`.

The test that fails without it: `apps/api/tests/test_chat_door.py`.

### S87-65 · feat(api): Slack Socket Mode feeds the same door without a public URL

Phase B, wave 14, size M. Depends on: S87-64.

Acceptance:

- An envelope is acknowledged before processing and routed once across replicas and reconnects; off starts no loop

Where: `packages/adapters/src/tranibor_adapters/chat/slack_socket.py`.

The test that fails without it: `apps/api/tests/test_slack_socket_mode.py`.

The default for a personal installation.

### S87-66 · feat(api): a person's event rules decide what an event addressed to them does

Phase B, wave 14, size L. Depends on: S87-64, S87-59.

Acceptance:

- notify, propose, task or start, written only in a person session; an event written by someone else may only notify or propose (422 at write); an hourly cap per rule

Where: `apps/api/src/tranibor_api/services/event_rules.py`.

The test that fails without it: `apps/api/tests/test_event_rules.py`.

### S87-67 · feat(api): an event proposal waits in my pending list and only my session confirms it

Phase B, wave 15, size M. Depends on: S87-66.

Acceptance:

- Listed in /me/pending; confirmed only through a person session, re-checking the project right; filed as person:<email>; a token gets 403; expired after seven days

Where: `apps/api/src/tranibor_api/services/event_proposals.py`.

The test that fails without it: `apps/api/tests/test_event_proposals.py`.

### S87-68 · feat(api,orchestrator): the person a step is addressed to is told in person

Phase B, wave 13, size L. Depends on: S87-60, S87-58.

Acceptance:

- A personal notice outbox keyed by person, kind and reference; Slack direct message, e-mail and Web Push in the person's order; never fails the act; a marked item is withheld

Where: `apps/api/src/tranibor_api/services/personal_notices.py`.

The test that fails without it: `apps/orchestrator/tests/test_human_request_notices.py`.

### S87-69 · fix(adapters): a Slack notice carries links, not decision buttons that never post back

Phase B, wave 11, size S. Depends on: S87-57.

Acceptance:

- No Slack message carries a button other than the open link; the S3-07 row is corrected to what is proven

Where: `packages/adapters/src/tranibor_adapters/notify/slack.py`.

The test that fails without it: `packages/adapters/tests/test_slack_notifier.py`.

### S87-70 · feat(api): inbound webhooks reach a personal installation through its relay hostname

Phase B, wave 14, size S. Depends on: S87-64, S87-43.

Acceptance:

- Webhook paths are public on the relay listener and internal routes stay 404; a readiness item checks reachability; a signed delivery through the relay is verified at the install

Where: `apps/api/src/tranibor_api/services/readiness.py`.

The test that fails without it: `tests/relay/test_webhook_through_relay.py`.

### S87-71 · feat(web): my chat accounts, event rules, proposals and notice channels

Phase B, wave 16, size M. Depends on: S87-60, S87-66, S87-67, S87-68.

Acceptance:

- A person links accounts, writes rules (task and start hidden for events from anyone), confirms or refuses proposals and orders notice channels; every new /me route has a client

Where: `apps/web/src/app/me/event-rules/page.tsx`.

The test that fails without it: `tests/web/test_api_routes_have_a_client.py`.

### S87-72 · docs(procedures): set up Slack, the Teams bot and the Notion webhook

Phase B, wave 16, size S. Depends on: S87-64, S87-65, S87-70.

Acceptance:

- Each chat connector type has its procedure and page, including what a personal installation needs

Where: `packages/core/src/tranibor_core/procedures`.

The test that fails without it: make procedures-check.

**Phase C — scenario 3, a person responsible for a request.**

### S87-73 · docs(adr): 0233, a work item has a responsible person

Phase C, wave 17, size S. Depends on: S87-00.

Acceptance:

- Indexed as proposed; amendment notes on ADR 0153 §1/§5 and 0196 §1-2; ADR 0077 §2 kept

Where: `docs/adr/0233-a-work-item-has-a-responsible-person.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

### S87-74 · docs(adr): 0234, a responsible person's step runs at their runner, under their rights

Phase C, wave 17, size S. Depends on: S87-01, S87-02.

Acceptance:

- Indexed as proposed; amendment notes on ADR 0097 §1, 0107 §3 and 0077 §3; lifts ADR 0147's refused personal device for an accepted responsible person only; a legal reading recorded before subscriptions serve a shared project

Where: `docs/adr/0234-a-responsible-person-s-step-runs-at-their-runner.md`.

The test that fails without it: `tests/docs/test_adr_index.py`.

ADR 0061 §3 kept.

### S87-75 · contract(openapi): a work item has a responsible person, with a history

Phase C, wave 18, size M. Depends on: S87-73.

Acceptance:

- contract-change: WorkItem.responsible (offered or accepted), its history, audit and event; a page reads them in one query; initiated_by untouched

Where: `apps/api/src/tranibor_api/db/models/work_items.py`, `packages/contracts/openapi.yaml`.

The test that fails without it: `apps/api/tests/test_work_item_responsible.py`.

### S87-76 · feat(api): take charge, hand over, accept, refuse and release a work item

Phase C, wave 19, size L. Depends on: S87-75.

Acceptance:

- accepted only from a person session; taking a held item needs the holder's hand-over or a project owner; removing a member releases what they held; every gesture audited

Where: `apps/api/src/tranibor_api/services/responsibility.py`.

The test that fails without it: `apps/api/tests/test_work_item_responsibility.py`.

### S87-77 · feat(api): each door sets the responsible person at birth

Phase C, wave 20, size M. Depends on: S87-76, S87-67.

Acceptance:

- The console's me or a member's e-mail; a confirmed launch or event proposal makes the confirming person accepted; a trigger's pointer offers; an unknown person is refused

Where: `apps/api/src/tranibor_api/services/tickets.py`.

The test that fails without it: `apps/api/tests/test_work_item_responsible_at_birth.py`.

### S87-78 · feat(adapters,api): a forge assignee with a proven link is offered the work item

Phase C, wave 19, size M. Depends on: S87-75.

Acceptance:

- GitHub, GitLab and Forgejo assignees with a link are offered, never accepted; a tracker change after an acceptance is journaled, not applied

Where: `packages/core/src/tranibor_core/domain.py`.

The test that fails without it: `apps/api/tests/test_tracker_assignee_responsible.py`.

Jira and Azure Boards wait for linked providers.

### S87-79 · contract(schemas): a human step and an exclusion may name the work item's responsible person

Phase C, wave 19, size M. Depends on: S87-75.

Acceptance:

- contract-change: user and excluded accept {{ work_item.responsible }}, resolved when a request is answered or listed; a hand-over re-addresses waiting steps

Where: `packages/contracts/schemas/workflow.schema.json`.

The test that fails without it: `apps/api/tests/test_human_actor_responsible.py`.

### S87-80 · feat(api,web,cli): my work, on the board, the item page and in the terminal

Phase C, wave 20, size M. Depends on: S87-76.

Acceptance:

- responsible filter, GET /me/work-items, the board's Mine view, the item page's gestures and the CLI's items mine, claim, accept and release

Where: `apps/api/src/tranibor_api/routers/my_work.py`.

The test that fails without it: `apps/api/tests/test_my_work.py`.

### S87-81 · feat(api,orchestrator): the accepted responsible person is the run's work owner and on-behalf subject

Phase C, wave 20, size M. Depends on: S87-76, S87-18.

Acceptance:

- For an accepted responsible person, work_owner and on_behalf_of name them and restricted bases are judged with their rights; a hand-over refuses the previous subject's reads

Where: `apps/api/src/tranibor_api/knowledge/on_behalf.py`.

The test that fails without it: `apps/api/tests/test_knowledge_on_behalf.py`.

### S87-82 · feat(orchestrator): a shared project's step runs at its responsible person's runner

Phase C, wave 21, size L. Depends on: S87-81, S87-51, S87-74.

Acceptance:

- The project's policy opts in; a run waits for the runner and its person is told, falls back to the platform only when the project chose it, and is never offered to another member's runner; a subscription in a shared project needs a separate organisation switch, off by default

Where: `apps/orchestrator/src/tranibor_orchestrator/activities/placement.py`.

The test that fails without it: `apps/orchestrator/tests/test_placement_owner.py`.

The legal reading of ADR 0234 comes before the switch is offered.

### S87-83 · feat(api): a hand-over cancels what runs for the previous holder

Phase C, wave 22, size S. Depends on: S87-82.

Acceptance:

- On hand-over or release the previous holder's runs are cancelled through the cancel lane, claimed ones included, and a late result is refused

Where: `apps/api/src/tranibor_api/services/responsibility.py`.

The test that fails without it: `apps/api/tests/test_work_item_responsibility.py`.

### S87-84 · feat(api): who ran a step on their own machine does not approve what it proposed

Phase C, wave 20, size S. Depends on: S87-18, S87-11, S87-74.

Acceptance:

- Outside the personal posture, the owner of the runner that executed a run cannot approve the governed actions it proposed

Where: `apps/api/src/tranibor_api/services/actions.py`.

The test that fails without it: `apps/api/tests/test_actions_personal_runner_sod.py`.

The sole member of a personal installation keeps S87-11's rule.

### S87-85 · feat(auth): a person enrols their runner with the device flow

Phase C, wave 18, size M. Depends on: S87-15, S87-74.

Acceptance:

- client_kind runner; the fingerprint shown and approved under fresh authentication; a single-use session usable only on the enrolment routes

Where: `apps/api/src/tranibor_api/routers/device_sign_in.py`, `packages/outpost/src/tranibor_outpost/personal.py`.

The test that fails without it: `apps/api/tests/test_device_runner_enrolment.py`.

For a hosted deployment; the personal installation enrols with an owner ticket.

### S87-86 · feat(chart): personal runners on a hosted deployment

Phase C, wave 19, size M. Depends on: S87-16, S87-85.

Acceptance:

- Runs enabled only with a run door hostname; the door gets the run-token key only then; prod and staging presets leave them off

Where: `charts/choregos/values.yaml`.

The test that fails without it: the chart render tests under charts/choregos/tests.

Proving the run door on a real ingress stays S65-02's job.

### S87-87 · feat(api): a project trigger may take a verified chat event and offer the item to the linked member

Phase C, wave 21, size M. Depends on: S87-64, S87-77.

Acceptance:

- A trigger bound to a chat connector opens a work item in the internal tracker and offers it to the member the proven account names; they act for nobody until they accept

Where: `apps/api/src/tranibor_api/services/triggers.py`.

The test that fails without it: `apps/api/tests/test_triggers_from_chat.py`.
