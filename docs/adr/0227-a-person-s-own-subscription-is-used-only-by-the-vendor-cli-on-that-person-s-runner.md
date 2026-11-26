# 0227 — A person's own model subscription is used only by the vendor's unmodified CLI on that person's runner; the platform never holds it

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends
  [ADR 0107](0107-an-agent-acts-under-an-identity.md) §6 and §7, [ADR 0004](0004-cost-counted-at-the-gateway.md),
  [ADR 0047](0047-a-budget-is-a-reservation.md) §1, [ADR 0010](0010-gates-are-mechanisms.md) (the row "no
  budget overrun"), [ADR 0029](0029-a-platform-for-governed-agents.md) §5 (the invariant "no secret enters an
  agent's pod"), plan decision D6 ([00-index.md](../plan/00-index.md), its row amended in the same pull
  request), [ADR 0134](0134-every-person-has-a-personal-space-conversations-are-durable.md) §3, §5 and §6,
  [ADR 0080](0080-hosted-prompt-and-voice-agents.md) §10 (S41-11) and the table of
  [ADR 0141](0141-tokens-and-the-context-are-read-per-scope.md); builds on ADR 0226, the personal runner)
- **Concerns**: the sealed outpost-run assignment (`packages/contracts/schemas/outpost-run.schema.json`,
  S87-03) and the `runs` row (`credential`, `executed_at`); the orchestrator's `prepare_stage` (`place_run`,
  `_reservation`, `reserve_key`), `start_run` (`_gateway_key`) and `book_spend`; `ClaudeCodeBackend.model_env`
  and the runner's ACP client (`acp/client.py`); the daemon `choregos-outpost` (`personal login`); the runner's
  client to the platform (`InternalClient`, `packages/tools-mcp/src/tranibor_tools_mcp/client.py`);
  `SECRET_PATTERNS`, `redaction.py`, `SECRET_FILE_PATTERNS`; `DirectGateway`; `services/network.py`; the cost
  ledger's `kind`; conversations (`services/conversations.py`, `services/agent_sessions.py`,
  `routers/agent_session_pods.py`); `demo/`

## Context

A person who runs Choregos for themself wants its agents to work on their own Claude subscription. They run
`claude setup-token`, which signs them in through Anthropic's own flow and prints a long-lived token for
`CLAUDE_CODE_OAUTH_TOKEN`. The vendors' terms decide where that token may go.

- **Anthropic.** The Claude Code documentation's "Legal and compliance" page, section "Authentication and
  credential use" (read 2026-11-26): "Anthropic does not permit third-party developers to offer Claude.ai
  login into their own applications, or to route requests through Free, Pro, Max, or Ultra plan credentials
  on behalf of their users. Moreover, developers may not collect, store, or intermediate Claude.ai
  credentials or session tokens — sign-in to a Claude account must complete through Anthropic's own flow."
  The same section does not "prevent an end user from signing in to the unmodified Claude Code binary with
  their own Claude subscription". The page asks that Claude Code be "run as published by Anthropic", with no
  authentication method removed or disabled, and says a plan's advertised limits "assume ordinary,
  individual usage of Claude Code and the Agent SDK".
- **Google.** The Gemini CLI's terms of service and privacy notice say that "Directly accessing the services
  powering Gemini CLI (for example, the Gemini Code Assist service) using third-party software" breaks the
  applicable terms and may be grounds for suspending the account.

What the repository did is the pattern those terms forbid:

- The demo (`demo/README.md`) copies `claudeAiOauth.accessToken` out of `~/.claude/.credentials.json` into a
  cluster-wide secret, `agent-creds`, which `demo/values-demo.yaml` hands to every run's pod
  (`envFromSecrets`) in `direct` mode. The platform collects the token, stores it and serves it to every run.
- The bench of 2026-09-25 ran on such a token (`docs/plan/status/history.md`, series `g`). Claude Code's own
  rotation revoked it mid-series (ten runs of eleven died), and the archived Temporal histories held it:
  "the gateway key (in `direct` mode, the OAuth token itself) and the run tokens were in them", redacted by
  hand at archiving.
- Without a gateway key, `Backend.anthropic_env` (`packages/runner/src/tranibor_runner/backends/base.py`)
  leaves the pod's authentication variables alone, so that "the agent brings its own credentials
  (subscription, provider key placed in the pod)". ADR 0146 §7 journals that case as the one exception to
  "no credential of the run in the agent's container".

ADR 0226 gives the execution place: a person's own outpost claims that person's runs. This record decides
which credential such a run may use, and what keeps the platform from ever holding it.

## Decision

1. **A run's credential source is `gateway` or `subscription`, and it never enters `StageInput`.** It travels
   in the sealed outpost-run assignment (`outpost-run.schema.json`: `credential`, `provider`), which only the
   claiming instance's key opens (`tranibor_adapters.sealing`), and on the `runs` row, which the Run DTO
   shows beside `executed_at`. `StageInput` and `ModelRef` do not change: they are `Strict` with
   `extra="forbid"`, and a field there would need a runner minimum-version gate. Every run today is
   `gateway`, and stays so.
2. **A subscription is used at one place: the personal outpost of the run's `work_owner`.** `place_run`
   (ADR 0226), called in `prepare_stage` before model resolution and before any reservation, chooses
   `subscription` only when all of these hold: the run executes at the personal outpost whose owner is the
   run's `work_owner` (the personal space's owner in phase A; the accepted responsible person in phase C,
   under an organisation switch of its own, off by default); the project's effective
   `sandbox.personal_runner.credential` lists `subscription`; an instance of that outpost announces the
   provider. Never the initiator's runner as such, never another member's. The personal posture (ADR 0228)
   defaults to `required` and `subscription`; elsewhere the default lists `gateway` only. Placement records
   `credential` and `executed_at` on the run and journals `run.placement`.
   *Refused*: a subscription run on any platform executor. `start_run` refuses it, not retried
   (`subscription_needs_personal_runner`), before anything reaches a backend. The runner repeats the
   refusal on its own (an exit code of its own in `tranibor_core/exit_codes.py`, `run.failed` with
   `phase: credential`) when a subscription assignment reaches it from anything but its daemon's claim.
   Tests: `apps/orchestrator/tests/test_placement.py` (personal space and standard project, runner online or
   not, provider announced or not), `packages/runner/tests/test_subscription_run.py::test_refused_off_an_outpost`.
3. **The platform reserves nothing, mints no key and writes no token.** For a subscription run,
   `prepare_stage` skips `_reservation`, `_refuse_on_budget` and `reserve_key`; `start_run`'s `_gateway_key`
   returns nothing; the run token is minted at claim (ADR 0226), never at start. The model is the provider's
   id, from a subscription map of the profiles (`ModelResolver` would resolve `platform/standard` to a
   gateway alias the vendor does not know), and `ClaudeCodeBackend` pins it. `Budget.usd` keeps a nominal
   value because the contract requires it (`tranibor_contracts.stage.Budget`, `usd > 0`); nothing reserves
   or mints from it, and the run page labels it "not a budget". Test:
   `apps/orchestrator/tests/test_subscription_spend.py::test_no_key_is_minted_and_no_reservation_made`.
4. **The token lives on the runner, and reaches only the vendor CLI of a subscription run.** The person
   gives what `claude setup-token` printed to `choregos-outpost personal login claude`, which reads it from
   standard input only (never an argument, never an environment variable), checks its shape and stores it in
   the OS keychain (macOS Keychain, Secret Service) or, without one, in a runner-local file at mode 0600.
   `choregos local runner login claude` and the desktop app's native "Connect Claude" window (ADR 0229)
   pass standard input through without reading it; no page the platform serves has a field for it.
   `doctor` reports its presence, never its value. The daemon announces the provider only while a token is
   present, and puts it only in the environment of a subscription run's vendor CLI (in docker mode the
   agent's container, passed by name, never in argv): ADR 0146 §7's exception, as written. Tests:
   `packages/outpost/tests/test_vendor_secrets.py` (never in a log, `doctor` or the journal; the file is
   0600), `packages/runner/tests/test_claude_subscription.py::test_the_agent_env_has_the_token_and_no_gateway`.
5. **The vendor's CLI runs unmodified, signed in by the person, and nothing else chooses its credential.**
   The agent is `claude-agent-acp` at the version `backends/versions.lock` pins, which runs the published
   Claude Code. A subscription run opens no model listener (`loopback/model.py`'s `ModelProxy`): the CLI's
   TLS goes to the provider through the CONNECT tunnel, which no platform component terminates.
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are removed from that environment
   even when the runner's own has them, so a key cannot silently take precedence; this selects the
   credential the run was placed for and changes nothing in the CLI. The person's own `~/.claude` is not
   read: the CLI runs under a runner-owned `CLAUDE_CONFIG_DIR` and the forced `HOME` of
   `workspace.agent_home`, so none of their MCP servers, hooks, plugins or `CLAUDE.md` joins a governed run.
   The platform never calls ACP `authenticate` and never offers the `claude-ai-login` method (`acp/client.py`
   sends `initialize` and no `authenticate`); a missing sign-in is detected and reported to the person,
   never repaired. Tests: `packages/runner/tests/test_subscription_run.py::test_no_model_listener_for_a_subscription_run`,
   `packages/runner/tests/test_claude_subscription.py` (no `ANTHROPIC_*` variable; the config directory is
   the runner's).
6. **The token is kept off the platform by mechanisms, each with its test (ADR 0010, ADR 0101).**
   - *Shapes.* One module, `tranibor_core/subscription_credentials.py`, names them (`sk-ant-oat01-`,
     `sk-ant-ort01-`, `ya29.`, the `claudeAiOauth` key) with `contains` and `scrub`. They join
     `SECRET_PATTERNS` (`tranibor_core/gates/__init__.py`, so `no_secrets` sees them) and `redaction.py`;
     `SECRET_FILE_PATTERNS` (`tranibor_runner/guardrails.py`) gains `.credentials.json` and `oauth_creds.json`.
     Test: `packages/core/tests/test_subscription_credentials.py`.
   - *Refused at ingress.* A request under `/api/v1/internal` whose URL or body holds a shape is refused
     (422 `subscription_credential_refused`), counted, and neither stored nor logged.
   - *Refused on writes.* Connector, organisation secret, agent `launch.env` and `LaunchSpec.env` writes
     refuse a shape with the same 422; `DirectGateway` refuses a shaped key in every environment; the worker
     refuses to start with a shaped value in its environment. Test:
     `apps/api/tests/test_subscription_credential_refused.py`.
   - *Scrubbed by exact value in the runner's single client to the platform.* Every URL and body the runner
     sends goes through `InternalClient`, whose transport replaces the token's exact value and its shapes:
     events, results, outputs, tool calls, questions, scope changes, the MCP relay, conversation deltas and
     errors that carry stderr. Each hit is journaled `subscription.token_redacted`, with a count only. Test:
     `packages/runner/tests/test_subscription_run.py::test_the_fake_agents_echo_of_the_token_is_scrubbed` (a
     fake ACP agent echoes the value on every path; a recording transport sees none).
   - *Refused before a push.* The runner refuses to push a diff that holds the value or a shape. Test:
     `packages/runner/tests/test_subscription_run.py::test_push_refused_when_the_diff_holds_the_token`.
   - *No Docker socket.* Where subscription runs are allowed, no platform container mounts the Docker
     socket: its holder could `docker inspect` the agent's container and read its environment. Test:
     `tests/deploy/test_personal_compose.py::test_no_platform_service_mounts_the_docker_socket`.
   - *The canary.* `tests/e2e/test_subscription_never_reaches_platform.py`: a fake ACP agent reads a canary
     `CLAUDE_CODE_OAUTH_TOKEN` and leaks it into its text, stderr, a finding, a question, a tool call's
     arguments, an output and a committed file. The canary, in clear, base64 and URL-encoded, must then be
     absent from every table of the database, every decoded Temporal history, the `docker inspect` of every
     platform container, every platform log, the object store and the relay's log; a positive control
     proves the agent received it. It runs in process on every pull request and nightly against the
     personal bundle; S87-35 adds a full conversation turn. Removing any one mechanism above turns it red.

   No Temporal codec rewrites payloads to scrub them (see the alternatives).
7. **A subscription line in the ledger, outside every USD budget; the other caps stay.** `book_spend` writes
   a line of kind `subscription`: cost 0, unmeasured, the provider, the model marked `(subscription)`. The
   costs page shows it apart. No USD cap counts it: not the work item's, the project's day, the
   organisation's month, the agent's day or month, nor an assistant's `per_conversation_usd`,
   `per_user_month_usd` or `per_installation_month_usd`. What still bounds the run: `max_turns`
   (`TurnGuard`), `max_minutes` (the orchestrator's timeout, then the outpost's cancel lane) and the
   tool-call caps. The run page says "your subscription: not measured; turns ≤ N, minutes ≤ M", with no
   dollar figure. Test: `apps/orchestrator/tests/test_subscription_spend.py::test_the_ledger_line_is_subscription_and_the_project_day_is_untouched`.
8. **Signed out or at its limit, a run stops for its person and is never retried.** Signatures taken from
   the spike's fixtures (S87-02, `packages/runner/tests/fixtures/subscription/`) are read for subscription
   runs only, in `tranibor_runner/budget.py` beside `GATEWAY_SIGNATURES`, and give the reasons
   `subscription_login_required` and `subscription_limit`. A patch marker modelled on `BUDGET_NEVER_RETRIES`
   sends both to the person (sign in on your runner; your plan resets at a time, when it is known), never
   back to the stage and never to the organisation's gateway, which would change who pays. Histories
   recorded before the marker replay unchanged. A conversation turn ends `subscription_unauthorized` or
   `subscription_limited`. Tests: `packages/runner/tests/test_subscription_signatures.py`,
   `apps/orchestrator/tests/test_subscription_never_retries.py` (with a replayed history).
9. **The provider's hosts are implied in egress for subscription runs only.** `project_effective_network`
   (`services/network.py`) implies the hosts the spike measured only when the run's credential is
   `subscription`. A deny list wins, and placement then refuses the subscription. A gateway run never gets
   a provider host. Test: `apps/api/tests/test_network_subscription_hosts.py`.
10. **A conversation turn on the runner follows the same rules.** A conversation placed `personal_runner`
    (ADR 0226) runs each turn through Claude Code on its owner's subscription, with the platform's MCP tools
    only: the built-in tools are denied twice, in the runner-owned `settings.json` and by the ACP permission
    handler, which allows `mcp__choregos__*` only. The turn's credential is a `turn-{conversation}-{n}` run
    token minted at claim; nothing is reserved, no key is minted, and the turn books one `subscription` line.
    A personal installation makes no model call on the platform side: a title is cut from the first message
    (`derive_title`), knowledge search is full text (`get_embedder` returns nothing without a key), and a
    conversation eval that needs a judge is refused with that reason. Tests:
    `apps/api/tests/test_conversation_on_runner.py::test_opening_on_the_personal_runner_asks_no_gateway`,
    `packages/runner/tests/test_serve_turn.py`.
11. **The demo's `agent-creds` is retired, and a test keeps it out.** The secret, the `jq` extraction from
    `~/.claude/.credentials.json` and the subscription path of `DEMO_GATEWAY=direct` go; `direct` stays usable
    in dev with a provider API key. Test: `tests/dev/test_no_platform_held_subscription.py` scans `charts/`,
    `demo/`, `compose.yaml`, `.env.example` and `examples/` for `CLAUDE_CODE_OAUTH_TOKEN`, `agent-creds` and
    `claudeAiOauth`; it fails on today's demo.
12. **Claude only in phase A.** `GeminiCliBackend` (`backends/others.py`) writes `GEMINI_API_KEY` and
    `selectedAuthType: api-key` for every run. Placement refuses the `google` provider until a spike shows a
    Google sign-in path the Google terms allow for this use, and a record of its own decides it.

## What is refused, and what is only recorded

| Case | Answer | Where |
|:--|:--|:--|
| A subscription run on a platform executor | refused, not retried | `start_run`; repeated by the runner |
| A subscription run at an outpost that is not its `work_owner`'s own | refused | `place_run` |
| A subscription value at `/api/v1/internal`, in a connector, a secret or a launch environment | refused, 422 | the ingress and the write routes |
| A subscription value as `DirectGateway`'s key or in the worker's environment | refused at start | `DirectGateway`, the worker's start |
| A push whose diff holds the value or a shape | refused | the runner |
| A platform container mounting the Docker socket where subscription runs are allowed | refused by a test | `tests/deploy/test_personal_compose.py` |
| The person's `~/.claude` (MCP servers, hooks, plugins, `CLAUDE.md`) | never read | runner-owned `CLAUDE_CONFIG_DIR` |
| ACP `authenticate`, the `claude-ai-login` method | never called, never offered | `acp/client.py` |
| What the subscription spent | recorded: cost 0, unmeasured, apart | the ledger, kind `subscription` |

Recorded, and not prevented:

- **The vendor account is not tied to the person.** Nothing ties the Claude account whose token sits in the
  runner's keychain to the Choregos person who owns the outpost: on a machine two people share, the other's
  sign-in would serve the owner's runs. `doctor` shows the vendor account when the CLI exposes it
  (unverified until the spike).
- **The terms are read, not proven.** The mechanisms prove the token never reaches the platform; they do not
  prove the use is permitted. This record reads the page as allowing a person's own work, on their own
  machine, through the published Claude Code, which `claude-agent-acp` runs through the Agent SDK. A shared
  project's request on a personal plan (phase C) needs a legal reading before it ships, and that reading
  covers this one too. The caps bound each run, not the person's day: "ordinary, individual usage" is the
  person's to keep.
- **Shapes are not complete.** A transformed copy of the token (split, compressed, encoded otherwise) passes
  the shape checks; the exact-value scrub catches verbatim copies; the canary proves the known paths, not
  every path.

## Unchanged

- [ADR 0109](0109-an-organisation-brings-its-own-model-gateway.md) §1: `direct` stays refused in staging and
  prod. A subscription is not a gateway: it never goes through `DirectGateway`, which now refuses a
  subscription-shaped key in every environment.
- [ADR 0146](0146-the-runner-and-the-agent-do-not-share-a-container.md) §7: its journaled exception ("a
  deployment without a gateway key, whose agent signs in with a credential of its own") applies as written;
  here the only credential that uses it is the person's own, on their own runner.
- [ADR 0062](0062-remote-agents-over-a2a.md) §6 is the precedent for a line kept apart and unmeasured
  (`kind = external`); a subscription line is kept apart the same way, and reserves nothing because nothing
  is declared.

## Alternatives rejected

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| A Temporal codec that scrubs subscription shapes from every payload | a leak gets past the runner's client | always: it rewrites payload bytes silently, a false positive changes a workflow's data, and its cost grows with every payload | **Rejected.** The single client and the ingress refusal keep the token out; the canary decodes every history to prove it |
| The `direct` gateway with the token as its key | there is no runner on the person's machine | the token sits in the run's Secret and in Temporal payloads: the pattern `history.md` recorded | **Rejected**; ADR 0109 §1 unchanged |
| A platform executor (the dev Compose worker with the Docker socket) running subscription runs | one install and no daemon | whoever holds the socket reads the agent container's environment | **Rejected** |
| The runner's `ModelProxy` injecting the token | the platform keeps a view of model traffic | platform code intermediates the credential | **Rejected** |
| ACP `authenticate` or `claude-ai-login` offered by the runner or the console | a sign-in button | that is offering Claude.ai login, which the terms forbid | **Rejected**; the person signs in with the vendor's CLI |
| Copying `~/.claude/.credentials.json` into the run's home | no `setup-token` step | it spreads the credential and breaks its refresh | **Rejected**; a read-only passthrough only if the spike requires it, by a record of its own |
| `ModelRef.credential` in `StageInput` | the runner reads it from its input | a `Strict` schema: every runner needs a minimum-version gate | **Rejected**; the sealed assignment carries it |
| A "would-have-cost" estimate counted against USD budgets | a familiar figure | it is a declared cost, which ADR 0147 refused | **Rejected**; the line is unmeasured and apart |
| Falling back to the organisation's gateway at the plan's limit | the work goes on | it changes who pays without a person deciding | **Rejected**; the run stops for its person |

## Consequences

- A person with a subscription and no API key gets agents and console chat on a personal installation with no
  provider key anywhere on the platform side.
- The platform measures nothing of that use: no money figure, no `context.breakdown` (ADR 0141), no
  gateway-side guard on the model traffic. Turns, minutes and tool-call caps are what bound it, and the
  injection guard on tool answers stays.
- Stories S87-14 to S87-19, S87-22, S87-28, S87-29 to S87-37 and phase A2's `chore(demo)` build it. This
  record proves nothing; their tests do, and each guarantee above names the test that will hold it.
