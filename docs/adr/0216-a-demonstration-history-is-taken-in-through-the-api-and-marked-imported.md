# 0216 — A demonstration history is taken in through the API, and marked imported

- **Status**: accepted, 2026-11-24; amended 2026-11-25 (points 7 to 11) and 2026-11-27 (point 12)
- **Concerns**: `apps/api` (`routers/demo_history.py`, `services/demo_history.py`, `rbac.py`, `services/budgets.py`,
  `routers/costs.py`, `routers/workitems.py`, `routers/agents/metrics.py`, `services/agents.py`, `metrics.py`,
  `services/projects.py`), `apps/web`
  (`components/imported-badge.tsx`), `packages/contracts` (`demo-history.schema.json`, `DemoHistory`,
  `openapi.yaml`), `packages/core` (`production.py`), `packages/cli` (`tranibor demo import`, `tranibor demo
  remove`); follows
  [ADR 0212](0212-the-api-can-run-the-workers-inline-for-a-demonstration.md) and
  [ADR 0213](0213-the-demonstration-mcp-server-is-the-fakes-with-tools-declared-in-a-file.md)

## Context

Building a pre-sales demonstration showed that no history can be written through the API. Runs, costs,
decisions and past states exist only because `dev/scripts/seed.py`, or a pack's own script, writes the
database directly. A demonstration on a deployed instance cannot do that, and every pre-sales team needs a
project that looks lived in: a board with work items in several states, a spend page with a month of cost.

The risk is the opposite one: a cost nobody measured, in the ledger that budgets, reports and exports read.

## Decision

1. **A declared document, taken in by one endpoint.** `POST /projects/{id}/demo-history` takes a
   `DemoHistory` (`choregos/DemoHistory/v1`, `schemas/demo-history.schema.json`): for an existing project,
   work items with fields, a state history, runs (agent, stage, status, duration, tokens, a result summary),
   decisions and findings. `tranibor demo import <project> <file>` posts a YAML or JSON file.
2. **A cost is declared, with its price.** A run gives `cost_usd`, or its tokens and the `prices` the document
   declares for its model (USD per million tokens, cached input apart). A model without a price is refused:
   the platform adds no price list of its own to a document.
3. **The gate is an opt-in, and production never opens it.** The instance must set
   `TRANIBOR_DEMO_HISTORY_IMPORT=1`; the fakes do not imply it (a deployed demonstration instance runs real
   adapters, not fakes, and still needs it). `staging` and `prod` refuse the import whatever the variable
   says (409 naming both settings), and the API does not start there with it on
   (`refuse_demo_history_import`, as ADR 0212 and 0213 refuse their modes). A new permission,
   `demo_history:import`, is held by the organisation administrator alone.
4. **The same builders as live data.** The workflow is the document's (`workflow`) or the project's default;
   every state named must exist in it; the work item is built by `new_work_item` (the one builder), its fields
   validated by the workflow's `metadata.inputs`; a decision by `new_human_request`; a state move is a
   `state_changed` event at its own instant, so the timeline reads like a live one. People must be members of the
   organisation and agents must be in its registry: the document cannot invent them. Everything is checked
   before the first row is written, and a refusal is a 422 naming the object.
5. **Idempotent by external id.** Every row's id derives (UUIDv5) from the project and the document's ids, and the
   work item keeps the digest of what it was imported from. The same document again writes nothing and reports
   everything `unchanged`; an id that already holds other content is a 409, never an overwrite; a new work item
   in a later document is added beside the first (ADR 0008).
6. **Marked, shown apart, counted nowhere.** `imported` (migration `7d2b9f4e6a10`) on `work_items`, `runs`,
   `findings` and `cost_ledger`, served by the API; every cost report row and total carries `imported_usd`; the
   ticket, project and organisation budgets, and the daily alert, ignore imported lines, so an imported day can
   never stop a real run. Imported work items have no interpreter and a key `demo:<id>`. Each import is audited
   as `demo_history.import`.

## Consequences

- A demonstration needs no database access. The same file gives the same history on every instance.
- The imported money is still in the ledger and in the spend reports (a demonstration must show a month of
  spend), but it is flagged, summed apart, and outside every budget.
- Rejected: a flag in the fakes only (a deployed demonstration cannot run on fakes); writing the rows through
  a new "import" path that skips validation (a demo that a live ticket could not reach would teach the wrong
  thing); a separate table of imported cost (the reports and exports would need a second source).

## Amendment, 2026-11-25: a request that still waits, the badge, the removal, the metrics

7. **A request may still wait** (`pending`, one per work item, `approval`, `question` or `scope_change`): the
   import writes the `human_requests` row a live step writes, without a transition, so the inbox
   (`pending_request`, `GET /me/pending`) lists it and anyone who may decide in the project answers it through
   `POST /work-items/{id}/decisions`. Refused (422) on a work item in a terminal state, asked before the work
   item was created, or under the id of one of its decisions. A `task` is not importable: its form writes the
   work item's fields.
8. **Deciding an imported request does nothing but record it.** The decision route reads the work item's
   `imported` column (the import's alone to set; a request's payload is not trusted) and, for an imported work
   item, passes the decision to no interpreter: the request is decided, the `human_decided` event and the
   `workitem.decision` audit entry carry `imported: true`, and nothing follows — no agent runs, nothing is
   written outside the platform, the work item stays in its state. Rejected: moving the work item along a
   transition the document would declare (a state the workflow did not reach, written by no workflow).
   `POST /work-items/{id}/actions` refuses every action on an imported work item (409), so `mark_agent_ready`
   can never start an interpreter, and its agents, on a past nobody lived.
9. **The console marks it**: an *Imported* badge on the board's cards, the ticket's page and its runs, the run's
   page, the agent's runs, the findings and the inbox; the ticket's page and the inbox say what deciding an
   imported request does.
10. **Removable**: `DELETE /projects/{id}/demo-history` and `tranibor demo remove` delete the rows marked
    `imported` in the project — the work items with their runs, requests, decisions and events (cascade), and
    the findings and cost lines by their own mark — and nothing else; the import's gate (permission
    `demo_history:import`, opt-in, never `staging` or `prod`); audited as `demo_history.delete` with the counts.
11. **Out of the agents' metrics**: `getAgentMetrics` and `listAgentMetrics` count no imported run and no
    imported cost line, and neither does the agent's spend (`spend_since`), which its daily and monthly budgets
    read. The DORA measures read deployments and releases, which an import never writes: nothing to exclude.
    The minimal option was chosen over a label in the response: a demonstration that showed an agent's lived
    metrics made of declared numbers would teach the wrong thing, and the history is still listed, marked.
12. **Out of the Prometheus gauges, and said in the CSV export** (2026-11-27): `metrics.refresh` counts no
    imported run (run counts by status, runs in flight, the start delay, permission denials, empty context
    packs), no imported cost line (cost by project, stage and model; today's cost) and no imported work item
    (lead time); the first-pass rate (`first_pass_merge_rate`, a gauge and the project card's
    `first_pass_acceptance_rate`) leaves imported work items out. The cost reports' CSV export keeps the
    ledger's sum in `cout_usd` and adds a last column, `imported_usd`, what an import put in that line, as
    the JSON report already did. Member spend reads the lines that name a person (`user_id`), which an import
    never writes: nothing to exclude. Proven by `apps/api/tests/test_imported_history_out_of_metrics.py`.

## Not done

- The project card's month cost (`ProjectStats.cost_month_eur`) still sums imported cost lines, as the cost
  reports' totals do (which show them apart, `imported_usd`); the card has no such field.
- A finding imported as `pending` can still be turned into a live work item ("Create the ticket"), which runs
  as any live one: that is a person's choice, not the import's.
- No version of the document beyond `v1`; a run's tool calls, artifacts and transcripts are not importable.
