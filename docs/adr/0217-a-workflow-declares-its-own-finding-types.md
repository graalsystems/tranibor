# 0217 — A workflow declares its own finding types, valid for its work items alone

- **Status**: proposed, 2026-11-24 (story S83-11, from a demonstration of a warranty-claims workflow;
  accepted when the project lead merges it)
- **Concerns**: the workflow schema (`metadata.finding_types`), the finding contract
  (`Finding.type`, `FindingRecord.type_title`), `tranibor_core.finding_types`, the validator, the run
  routes `postRunFinding` and `postRunResult`, the outcome activity, the console's findings page and
  the CLI's `findings list`; builds on [ADR 0181](0181-the-contracts-speak-no-trade.md) (the open
  lists, round C0) and [ADR 0029](0029-a-platform-for-governed-agents.md) (any trade)

## Context

A finding is a problem an agent, a step or a person reports outside the work in hand. Its `type` is an
open list since ADR 0181: the core keeps `security`, `docs` and `ux`, and an installed extension declares
more (the `software-delivery` plugin declares `bug`, `perf`, `tech-debt` and `flaky-test`). The list is
**process-wide**: `FINDING_TYPES.declare(value, plugin=)` runs when an extension loads, and a value is valid
for every project and every workflow of the deployment.

A pre-sales demonstration of a warranty-claims workflow showed what that leaves a trade: a suspected
duplicate claim had to be filed as `security`, for lack of a business type, and the console then listed it
under a name that meant something else. The product says any trade can run on it; a trade cannot add a
finding type without writing and shipping a plugin, and a plugin's list is global, so two workflows of two
trades that both want `duplicate-claim` would share one meaning, or collide (`OpenList.declare` refuses a
value another plugin declared).

## Decision

1. **A workflow declares finding types in `metadata.finding_types`**: a list (30 at most) of
   `{name, title, severities?, description?}`. `name` has the list's own pattern
   (`^[a-z][a-z0-9-]{0,31}$`), `title` is what the console shows (80 characters), `severities` is the
   scale a finding of that type may carry (a subset of `low`, `medium`, `high`, `critical`; absent: all
   four), `description` is free text. It is part of the workflow document, so it is published, versioned
   and restored with it, and a work item is judged against the version it was born in.
2. **The registry is extended, not paralleled.** `FINDING_TYPES` still holds the core's and the plugins'
   types, valid everywhere. A workflow's types are a second, **scoped** layer on top of it, resolved by
   `tranibor_core.finding_types.allowed(workflow)`: the registry's types, then the workflow's, the
   workflow's declaration first. Nothing a plugin declares changes, and the core's three types and the
   plugins' four stay valid in every workflow, declared or not.
3. **The contract reads a name; the platform judges it.** `Finding.type` is validated by the list's
   pattern alone, and a core type still comes back as its enumeration member. The contract cannot judge
   a workflow's type (it knows no workflow), and the runner and the interpreter already read a type
   unjudged (ADR 0181, `UNJUDGED`, `STORED`). The judgement is where the work item is known:
   - the API's `postRunFinding` and `postRunResult` answer **422 before writing anything**, with a
     message that names the workflow and lists the allowed types, or names the severity a type does not
     allow;
   - the outcome activity judges the findings a result carries (the ones the runner could not post) and
     does not file one the workflow does not allow, logging `finding.refused`.
4. **Publication refuses what would be ambiguous.** The validator reports `finding_types.duplicate` (a
   name declared twice in one workflow) and `finding_types.taken` (a name the core or an installed plugin
   already gives a type: it would mean two things depending on the workflow). A plugin installed after a
   workflow was stored may take the same name; the workflow's declaration then wins for its own work
   items, since a stored version is never rewritten.
5. **The API and the console show the title.** `FindingRecord` gains `type_title`: the title the
   finding's workflow gives its type, `null` for a core or plugin type and for a type its workflow no
   longer declares (the name is then shown). The list loads each workflow version once. The console shows
   the title with the name as a hover; the CLI shows `name (title)`.

## Consequences

- A trade names its problems in its own words without a plugin: `duplicate-claim`, `missing-proof`,
  `sla-breach`. The same name in two workflows is two declarations and cannot collide.
- A finding of a type its workflow later stops declaring stays readable (the row keeps the name); only a
  new finding of that type is refused.
- `Finding.type` no longer refuses an unknown name in the contract: a consumer that parsed a `Finding` to
  learn whether a type was known must ask `tranibor_core.finding_types.refusal`. The wire strings and the
  stored rows do not change, and no history changes (the interpreter already read types unjudged), so
  every archived history replays.
- Gates and humans file no finding today: the only routes in are an agent's (`report_finding`, the
  result's `findings`). When a gate or a person gains one, it judges with the same function.
- Not decided here: a severity default per type, a type's own triage policy (notification severities,
  auto agent-ready sizes), a template-level declaration shared by several workflows.

## What this proves and does not prove

It proves the contract, the validator, the judgement at the two API routes and the outcome activity, and the
titles in the API, the console and the CLI. It does not prove a real agent reporting a workflow's type end to
end through the runner's MCP tool, and it does not move the triage (deduplication, ticket creation), which
keys on the title and ignores the type.
