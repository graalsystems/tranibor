# Where Tranibor sits

An honest comparison, first written on 2026-09-25 from public sources and revised on 2026-11-23
for what Tranibor itself does today (each claim about Tranibor below names the code or the record
that proves it). It says what others do better; that is the part worth reading.

> **Dated note.** The statements about other products (google/ax, kubernetes-sigs/agent-sandbox,
> OpenHands, Tembo, the coding-agent products, the agent builders, treg, Obot, Docker MCP Gateway)
> date from **September 2026** and have not been re-checked since: they must be refreshed before
> anyone relies on them. Only the Tranibor columns were corrected against the code.

## In one sentence

Tranibor is a **general-purpose platform for governed agents** (ADR 0029, 2026-10-05): any
business process — software delivery, IT operations, joiners and leavers, a data change — carried
by agents and people through workflows whose gates refuse, whose evidence the platform measures,
and whose writes to third-party systems wait for a re-authenticated approval. It runs inside the
customer's perimeter, whatever the agent. It is not a coding agent, not a sandbox runtime, not an
identity provider and not a general-purpose MCP gateway: it consumes the one an organisation runs,
through MCP and standard authentication only ([ADR 0105](adr/0105-choregos-consumes-an-mcp-gateway-it-never-is-one.md)).

## What changed on 2026-10-05

Until then this page described a *delivery layer* — ticket in, controlled production change out
(ADR 0028). The product owner widened the scope: several workflows per project, an agent registry
with skills, MCP servers as connectors, connectors by capability, and governed actions in the
core. The comparisons below were written for the delivery layer and still hold for it; the new
competitors are the agent builders of the large platforms (Microsoft Copilot Studio, Google
Gemini Enterprise (formerly Agentspace), Dataiku's agent management). Against them Tranibor claims only what they do not ship
together: open source, self-hosted, any agent, and governance by mechanisms rather than prompts.
Each claim becomes true story by story (streams S15 to S20); until a story is delivered, this page
does not count it.

## Against the runtimes

| | google/ax | kubernetes-sigs/agent-sandbox | Tranibor |
| :-- | :-- | :-- | :-- |
| What it is | Task / Workspace / Gateway / Model primitives on GKE | `Sandbox`, warm pools, suspend/resume, `RuntimeClass` | stages, gates, evidence, ledger, trains |
| BYO agent | image | image | any ACP agent (ADR 0002) |
| Egress control | allowlist gateway | none (bring your own) | per-project Squid proxy from the policy, agent-side guardrails first |
| Credentials | per task | per sandbox | one capped virtual key per run; tool keys never reach the agent (ADR 0014), and the agent never holds the run token (ADR 0115) |
| Suspend / snapshot | yes | yes | yes, by sitting on agent-sandbox: the `agent_sandbox` executor, the default (ADR 0016, ADR 0072), suspends the sandbox across a human question and resumes the same run in its kept workspace; the agent's conversation is reloaded when the agent announces ACP `session/load`; otherwise a new one starts with the answer and a bounded, deterministic recap of the agent's own plan, last messages, calls and question, built from the kept journal (ADR 0223; `executor/agent_sandbox.py`, `runner.py` `_open_session`). An executor that cannot suspend replays the stage in a new run, which also reads the recap of the run that asked |
| Kernel isolation | gVisor | `RuntimeClass` | gVisor only where the policy or the deployment asks (`runner.runtimeClass`); `RuntimeDefault` seccomp is required on every pod by a Kyverno rule, and we ship no custom seccomp profile (`infra/policies/pod-security.yaml`, `docs/security.md`) |
| Stages, gates, evidence, cost, trains | no | no | yes |
| Control plane | cluster-scoped, unsupported by vendor | cluster-scoped, v1beta1 | namespaced; a tenant can install it |

Reading: ax and agent-sandbox solve problems Tranibor has (density, suspend), and Tranibor
solves problems they do not have (what is a stage, who signed off, what did it cost). The
`agent_sandbox` executor (ADR 0016, the default since ADR 0072) is the proof of that layering.

## Against the agent platforms

| | OpenHands | Tembo | Copilot / Codex / Devin / Factory / Cursor | Tranibor |
| :-- | :-- | :-- | :-- | :-- |
| Agent | its own | several harnesses | locked | any ACP agent |
| Trigger | chat, issue | ticket | chat, issue | ticket, with a workflow per ticket |
| Budgets | per project, built-in gateway | — | credits | per stage, hard cap on the key, tool calls in the same ledger |
| Prompt-injection detection | **yes, with automatic halt** | — | varies | lexical detection of known patterns over the ticket, its documents and the memory pack **before** the run, and over tool answers: `warn` journals a security event, `block` (the `regulated` preset) stops the stage; a net against crude cases, not an understanding, and no automatic halt mid-run (`tranibor_core/injection.py`, `apps/orchestrator/tests/test_injection.py`; `docs/security.md`) |
| Gates with refusal semantics | no | no | no | yes, statically validated (ADR 0010) |
| Evidence measured by the platform | partial | no | no | tests run by the runner, business facts, gate verdicts |
| Human-in-the-loop | chat | — | chat | a state with an SLA, a reminder and an escalation; the run resumes in its own sandbox, with the agent's conversation reloaded when the agent supports ACP `session/load`, else with a recap of its earlier work and the answer (who, when), through the injection guard (ADR 0223) |
| Self-hosted, GitOps-only | yes | SaaS | mostly no | yes, Apache 2.0 |
| Non-code work | no | no | no | yes (ADR 0012) |

Reading: OpenHands' injection detection (with its automatic halt) and the mature HITL chat of the
products are ahead on depth. Tranibor detects only the crude lexical cases; its answer to injection
is mainly mechanical (scope refused, diff reverted, secrets gated), and `docs/security.md` says the
walls, not the detector, are the guarantee.

## Frameworks live inside a step

Tranibor orchestrates; it is not an agent framework and does not ship one. A step may run a coding
agent, a prompt agent or code written on LangGraph, the OpenAI Agents SDK, Pydantic AI or any other
framework: the workflow (Temporal) owns durable state, retries and human gates, the step owns its own
control flow. The only thing a code agent needs from Tranibor is the run contract, through four
standards: ACP to the runner, MCP for tools, an OpenAI-compatible endpoint for the model, and the
result file. See [agents written in code](code-agents.md#why-no-choregos-framework).

## Against the tool brokers

| | treg | Obot / Docker MCP Gateway | Tranibor catalogue (ADR 0014) |
| :-- | :-- | :-- | :-- |
| Breadth | ~2,900 endpoints | MCP servers | what the deployment declares — a few tools |
| Credentials | server-side, one token per member | brokered | server-side; the agent holds no run token and no key (the run token stays with the runner, ADR 0115) |
| Real tool name and URL hidden from the agent | no | no | yes |
| Auto-discovery of tools | yes | yes | refused: 404, not 403 |
| Per-group access | — | partial | yes, two locks (deployment and project) |
| Cost per call in the same ledger as models | no | no | yes |

Reading: breadth is theirs. A federated catalogue (P3) would take entries *from* them, into
a PR a human merges.

A gateway is also something the table does not show: a hosting and credential layer. It runs the servers
that speak stdio, holds their upstream credentials and OAuth grants, and applies the network rules of the
servers' side. Tranibor can sit behind the one an organisation already runs: its broker calls the gateway's
servers with the same approvals, ledger and journal as for any other connector, and Tranibor holds none of
the credentials of the servers it serves. It never replaces a gateway and never becomes one ([ADR 0105](adr/0105-choregos-consumes-an-mcp-gateway-it-never-is-one.md)).

For tools in a network nothing outside may connect to, and for an organisation with no gateway there,
Tranibor ships an **outpost** ([ADR 0147](adr/0147-an-outpost-only-dials-out.md); [Outposts](outposts.md)).
Like a forge's self-hosted runner, it is a program installed inside that connects out; unlike one, it
runs no job and no code the platform sends, only the tool calls its own policy file allows, against
MCP servers its operator runs. It is not a tunnel either: the platform names an alias and a tool,
never a host. Runners and tunnels are mature and do more. An outpost does one thing, carries reads
only so far, gives no data residency, and has not run on a real network yet.

## Standards

- **ACP**: adopted (ADR 0002); more than fifty agents speak it.
- **MCP 2026-07-28**: the sidecar predates it (stateless core, `tasks`, elicitation,
  OAuth resource servers); conformance is a P3 item.
- **A2A**: both ways. As a client ([ADR 0062](adr/0062-remote-agents-over-a2a.md)), a registry agent of
  kind `remote` runs a workflow step over A2A (0.3 and 1.0), called from the runner pod, under the same
  gates, budget and journal as any step. As a server ([ADR 0078](adr/0078-governed-workflows-as-a2a-skills.md)),
  a workflow that opts in is a skill another agent or an application can start and follow, in A2A 0.3.0:
  [Starting a workflow from outside](starting-a-workflow-from-outside.md).
- **OpenTelemetry GenAI**: emitted, off until an OTLP endpoint is configured (ADR 0018, partially implemented): one trace per run, with `invoke_workflow`, `invoke_agent`, `execute_tool` and `chat` spans, never carrying prompts, tool arguments or results. The conventions are still "Development"; one module names them and a test pins them to the semantic-conventions package (`tranibor_core/telemetry.py`, `packages/core/tests/test_telemetry.py`; `docs/deployment.md`, "Traces").
- **SLSA / AI authorship**: ADR 0015.

## What to take away

Choose Tranibor when the question is *"what did the agent do, under which contract, who
signed off, what did it cost, and can I prove it?"* — and expect to bring, or wait for, a
runtime for density, a stronger detector for injection than a lexical net, and breadth for tools.
