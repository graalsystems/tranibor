# 0216 — A demonstration history is taken in through the API, and marked imported

- **Status**: accepted, 2026-11-24
- **Concerns**: `apps/api` (`routers/demo_history.py`, `services/demo_history.py`, `rbac.py`, `services/budgets.py`,
  `routers/costs.py`), `packages/contracts` (`demo-history.schema.json`, `DemoHistory`, `openapi.yaml`),
  `packages/core` (`production.py`), `packages/cli` (`tranibor demo import`); follows
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

## Not done

- The console shows no "imported" badge yet (the API serves the flag; `imported_usd` is in the Spend page's data).
- Agent metrics, the DORA measures and the other readers of the ledger count imported lines as spend: only the
  budgets and their alerts exclude them.
- No removal of an imported history; delete the work items, or the project.
- No version of the document beyond `v1`; a run's tool calls, artifacts and transcripts are not importable.
