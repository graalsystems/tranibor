# Where Tranibor sits

An honest comparison, first written on 2026-09-25 from public sources and revised on 2026-11-24
for what Tranibor itself does today (each claim about Tranibor below names the code or the record
that proves it) and for what the other products do (re-checked the same day, see the sources at
the end). It says what others do better; that is the part worth reading.

> **Dated note.** The statements about other products were written in **September 2026** and
> re-checked against public primary sources (official docs, repositories, release notes, vendor
> blogs) on **2026-11-24**; the sources are listed at the end of the page. What changed is marked in
> the tables below. What the 2026-11-24 pass could **not** verify, said plainly rather than guessed:
> whether ax is cluster-scoped and what support Google gives it; per-task credential scoping in ax;
> whether agent-sandbox resources are namespaced (the docs imply it, none states it); OpenHands'
> per-project budgets and its built-in gateway (only budget management in OpenHands Cloud is
> documented); any budget control in Tembo (none documented); whether Cursor, Devin, Factory, Codex
> and Copilot hold budgets other than credits and usage allowances; the "Gates with refusal
> semantics", "Evidence measured by the platform" and "Non-code work" rows for every product (these
> are judgements about workflow semantics, not documented features); whether treg issues one token
> per member; whether Obot or Docker MCP Gateway hide the real tool name from the agent and put
> cost per call in a ledger shared with models; Docker MCP Gateway's licence on the docs page (the
> repository says MIT); whether Copilot Studio's human approval of tool calls is generally available
> (a roadmap entry only); and whether Dataiku Agent Management reached general availability. Read
> those cells as the September statement, not as a fresh finding.

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
Gemini Enterprise (formerly Agentspace), Dataiku's agent management). Checked on 2026-11-24,
they ship more governance than the September text allowed: Gemini Enterprise's builder (Agent Designer,
now the Workflow Builder) has human approval steps and administrator review before an agent is shared,
and Google folded its Agent Builder into the Gemini Enterprise Agent Platform (April 2026); Copilot
Studio ships default cross-prompt-injection protection; Dataiku added human-in-the-loop approval for
visual agents, agent review, guardrails and LLM cost blocking. Against them Tranibor claims only what
they do not ship together: open source, self-hosted, any agent, and governance by mechanisms rather
than prompts.
Each claim becomes true story by story (streams S15 to S20); until a story is delivered, this page
does not count it.

## Against the runtimes

| | google/ax | kubernetes-sigs/agent-sandbox | Tranibor |
| :-- | :-- | :-- | :-- |
| What it is | Task / Workspace / Gateway / Model primitives (`ax.io/v1alpha1`, Apache 2.0) on Kubernetes, running on Agent Substrate; the README warns of breaking changes before a stable release | `Sandbox`, warm pools, suspend/resume (Python SDK, on GKE Pod Snapshots), `RuntimeClass` | stages, gates, evidence, ledger, trains |
| BYO agent | image | image | any ACP agent (ADR 0002) |
| Egress control | allowlist gateway | none (bring your own) | per-project Squid proxy from the policy, agent-side guardrails first |
| Credentials | injected by the Gateway, never seen by the agent; per-task scoping not confirmed | per sandbox | one capped virtual key per run; tool keys never reach the agent (ADR 0014), and the agent never holds the run token (ADR 0115) |
| Suspend / snapshot | yes (`ax suspend` / `ax resume`, snapshots by Agent Substrate) | yes, but the documented path is Python-SDK-only and needs GKE Pod Snapshots on a gVisor node pool | yes, by sitting on agent-sandbox: the `agent_sandbox` executor, the default (ADR 0016, ADR 0072), suspends the sandbox across a human question and resumes the same run in its kept workspace; the agent's conversation is reloaded when the agent announces ACP `session/load`; otherwise a new one starts with the answer and a bounded, deterministic recap of the agent's own plan, last messages, calls and question, built from the kept journal (ADR 0223; `executor/agent_sandbox.py`, `runner.py` `_open_session`). An executor that cannot suspend replays the stage in a new run, which also reads the recap of the run that asked |
| Kernel isolation | gVisor through Agent Substrate (or Cloud Hypervisor microVMs); the AX README itself names none | `RuntimeClass` (gVisor, Kata) | gVisor only where the policy or the deployment asks (`runner.runtimeClass`); `RuntimeDefault` seccomp is required on every pod by a Kyverno rule, and we ship no custom seccomp profile (`infra/policies/pod-security.yaml`, `docs/security.md`) |
| Stages, gates, evidence, cost, trains | no | no | yes |
| Control plane | installed in the `ax-system` namespace; cluster scope and support terms not stated | v1beta1; resources appear namespaced (not stated) | namespaced; a tenant can install it |

Reading: ax and agent-sandbox solve problems Tranibor has (density, suspend), and Tranibor
solves problems they do not have (what is a stage, who signed off, what did it cost). Since
September, Google has released Agent Substrate 1.0 (open source, available on GKE, production
support by allowlist) and donated it, with gVisor, to the CNCF at Sandbox level (November 2026):
ax's runtime layer is now a community project rather than a Google-internal one. The
`agent_sandbox` executor (ADR 0016, the default since ADR 0072) is the proof of that layering.

## Against the agent platforms

| | OpenHands | Tembo | Copilot / Codex / Devin / Factory / Cursor | Tranibor |
| :-- | :-- | :-- | :-- | :-- |
| Agent | its own (Agent Canvas, SDK, any ACP agent) | several harnesses (Claude Code, Codex, Cursor, Amp, OpenCode, Pi) | mostly their own; Copilot also runs Claude and Codex agents (public preview) | any ACP agent |
| Trigger | chat, issue, automations (Slack, GitHub, Linear) | ticket, alert, schedule, webhook | chat, issue | ticket, with a workflow per ticket |
| Budgets | budget management in OpenHands Cloud (per-project gateway budgets not re-verified) | none documented | credits and usage allowances | per stage, hard cap on the key, tool calls in the same ledger |
| Prompt-injection detection | partial: an LLM-based or Gray Swan security analyzer rates each action's risk, and high-risk actions ask for confirmation; the analyzer itself does not block, and the docs say it is not a complete injection solution | — | varies (Copilot cloud agent: firewall allowlist, CodeQL and secret scanning of the result) | before the run, over the ticket, its documents and the memory pack: lexical detection of known patterns, then, if the deployment provides one, a classifier model on the texts the patterns did not flag (LLM Guard's API shape, off by default, ADR 0219); over tool answers, the lexical net only. `warn` journals a security event, `block` (the `regulated` preset) stops the stage and fails closed when the classifier is down; probabilistic, not an understanding, and no automatic halt mid-run (`tranibor_core/injection.py`, `tranibor_core/injection_classifier.py`, `apps/orchestrator/tests/test_injection.py`; `docs/security.md`) |
| Gates with refusal semantics | no | no | no | yes, statically validated (ADR 0010) |
| Evidence measured by the platform | partial | no | no | tests run by the runner, business facts, gate verdicts |
| Human-in-the-loop | chat, and confirmation policies (always, or risky actions only) | pull-request review, with a feedback loop that revises | chat, pull-request review | a state with an SLA, a reminder and an escalation; the run resumes in its own sandbox, with the agent's conversation reloaded when the agent supports ACP `session/load`, else with a recap of its earlier work and the answer (who, when), through the injection guard (ADR 0223) |
| Self-hosted, GitOps-only | yes (MIT; Cloud and Enterprise are commercial) | self-hosted single instance with a licence key (Kubernetes in early access); SaaS otherwise | varies: Cursor Enterprise self-hosted workers (Cursor still runs the agent loop and inference), Devin in-VPC environments (decisions stay in the vendor's tenant), Factory in your infrastructure or airgapped, Codex CLI local (Apache 2.0) with Codex cloud managed by OpenAI, Copilot cloud sandboxes with no self-hosted runners | yes, Apache 2.0 |
| Non-code work | no | no | no | yes (ADR 0012) |

Reading: OpenHands' risk analyzer with confirmation, and the mature HITL chat and review flows of the
products, are ahead on depth. (In September this page credited OpenHands with injection detection and
an automatic halt; its current documentation describes risk rating plus a confirmation prompt, not a halt.)
The products' self-hosting is also wider than the September text said, though in most the vendor
still runs the model loop; Tranibor's claim is the whole stack inside the perimeter, open source. Tranibor's own detection is a lexical net, with an optional classifier model
the deployment runs, never proven here against a real attack corpus; its answer to injection
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
| Breadth | 4,200+ endpoints across 140 providers | MCP servers | what the deployment declares — a few tools |
| Credentials | server-side; the agent holds one revocable treg token (per-member tokens not re-verified) | brokered (Obot keeps provider credentials and injects secrets at its egress gateway; Docker injects them from Docker Desktop's secret store) | server-side; the agent holds no run token and no key (the run token stays with the runner, ADR 0115) |
| Real tool name and URL hidden from the agent | no | no | yes |
| Auto-discovery of tools | yes (treg: the agent asks for a task, not a tool) | yes (Docker: dynamic discovery; Obot: catalogue search and device inventory) | refused: 404, not 403 |
| Per-group access | — | Obot yes (identity-provider groups, virtual MCP servers); Docker: profiles per client, no group control documented | yes, two locks (deployment and project) |
| Cost per call in the same ledger as models | no: priced per call in its own bill, apart from model spend | no | yes |

Reading: breadth is theirs (treg's catalogue grew by about half since September), and Obot's
group access is now as fine as ours. Docker's gateway is MIT-licensed on GitHub, while its governed
edition (AI Governance) is invite-only through sales. A federated catalogue (P3) would take entries *from* them, into
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
runtime for density, an injection classifier service of your own (Tranibor calls one, it ships none), and
breadth for tools.

## Sources (checked 2026-11-24)

- google/ax: <https://github.com/google/ax>; <https://raw.githubusercontent.com/google/ax/main/README.md>; <https://www.infoq.com/news/2026/09/google-ax-orchestrator/>
- Agent Substrate and gVisor: <https://opensource.googleblog.com/2026/11/sandboxing-untrusted-ai-agents-at-scale.html>; <https://cloud.google.com/blog/products/containers-kubernetes/agent-substrate-available-on-gke>
- kubernetes-sigs/agent-sandbox: <https://github.com/kubernetes-sigs/agent-sandbox>; <https://agent-sandbox.sigs.k8s.io/docs/getting_started/overview/>; <https://agent-sandbox.sigs.k8s.io/docs/sandbox/snapshots/>
- OpenHands: <https://github.com/OpenHands/OpenHands>; <https://docs.openhands.dev/overview/introduction>; <https://docs.openhands.dev/sdk/guides/security>
- Tembo: <https://www.tembo.io>; <https://docs.tembo.io/features/self-hosted/overview>; <https://tembo.io/pricing>
- GitHub Copilot: <https://docs.github.com/en/copilot/concepts/agents/about-third-party-agents>; <https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent>
- Cursor: <https://cursor.com/blog/self-hosted-cloud-agents>; <https://cursor.com/docs/cloud-agent/self-hosted-pool.md>
- Devin: <https://docs.devin.ai/enterprise/vpc/overview>
- Factory: <https://docs.factory.ai/enterprise/network-and-deployment>; <https://docs.factory.ai/enterprise/airgapped-deployment>
- Codex: <https://github.com/openai/codex>; <https://developers.openai.com/codex/enterprise/admin-setup>
- treg: <https://treg.to/>
- Obot: <https://github.com/obot-platform/obot>
- Docker MCP Gateway: <https://docs.docker.com/ai/mcp-gateway/>; <https://github.com/docker/mcp-gateway>
- Gemini Enterprise: <https://docs.cloud.google.com/gemini/enterprise/docs/workflow-builder>; <https://docs.cloud.google.com/agent-builder>; <https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform>
- Copilot Studio: <https://www.microsoft.com/security/blog/2026/01/23/runtime-risk-realtime-defense-securing-ai-agents/>; <https://microsoft.com/en-us/microsoft-copilot/blog/copilot-studio/strengthen-agent-security-with-near-real-time-protection-in-microsoft-copilot-studio>
- Dataiku: <https://doc.dataiku.com/dss/14/release_notes/index.html>; <https://doc.dataiku.com/dss/latest/release_notes/agent-hub.html>; <https://www.dataiku.com/product/agent-hub>
