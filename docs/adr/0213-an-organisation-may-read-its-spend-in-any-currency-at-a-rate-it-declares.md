# 0213 — An organisation may read its spend in any currency, at a rate it declares

- **Status**: accepted, 2026-11-23
- **Concerns**: `apps/api` (`display_currency.py`, `routers/costs.py`, `routers/projects.py`, `schemas/`),
  `packages/contracts` (`Org`, `OrgUpdate`, `CostReport`), `apps/web` (`lib/spend.ts`, the Spend page, the
  live board, the Organisation card), `packages/cli` (`money.py`); extends S29-03 (decision D4) and
  [ADR 0187](0187-the-workspace-is-one-screen-and-the-flow-is-live.md) §1

## Context

An organisation's currency was `EUR` or `USD` (S29-03, decision D4). A subsidiary that budgets in Mexican
pesos could not say so, and the console would show its spend in euros it does not use.

How the two currencies work today, and what that fixes for the design:

- **The ledger is measured in USD** (the gateway's price, `cost_usd`). Beside it every ledger line carries
  `cost_eur`, written at the same time at a deployment-wide rate (`fx_usd_eur`, 0.92 by default).
  The euro is therefore not an exchange rate the organisation chose: it is a second column of the ledger.
- **A report names the currency it is shown in** (`CostReport.currency`); the console picks the matching
  column (`amount()` in `lib/spend.ts`), and converts a budget, which is in USD, with the report's own
  EUR/USD ratio. The CLI does the same (`money.py`).
- There is no source of exchange rates in the platform, and adding one (a feed, a daily job, a vendor) is a
  commitment this story does not need: what a subsidiary needs is to read its spend in its currency.

## Decision

1. **The ledger stays in USD as measured, and nothing about it changes.** No column is added, no line is
   rewritten, `cost_usd` and `cost_eur` are served as before. A currency is a way of *reading* the ledger.
2. **An organisation's display currency is any ISO 4217 code.** `EUR` and `USD` keep reading the ledger's
   own columns and need no rate. **Any other code is USD converted at the rate the organisation declares**:
   `fx_rate`, the units of the currency per 1 USD, and the UTC date the server stamped when it was set
   (`fx_rate_set_on`). Both are organisation settings, set by its administrator in the same `PATCH
   /orgs/{org}` as the currency, and audited in `org.update`.
3. **There is no default rate and no fallback.** A code other than `EUR`/`USD` without `fx_rate` is refused
   (422), as is an unknown code, a rate that is not positive and finite, and a rate sent with `EUR` or `USD`
   (the ledger holds them: a second rate would contradict the first). A rate declared for one currency is
   never carried to another: changing `MXN` to `COP` without a new rate is refused. A stored currency that
   somehow has no usable rate reads as the ledger's `EUR` rather than as a guess of ours (`display_currency.read`).
4. **The conversion is the API's, once, and says so.** Every cost report line and total carries
   `cost_display`, in `CostReport.currency`: the ledger's EUR or USD column, or `cost_usd` times the declared
   rate rounded to the ledger's six decimals. `CostReport.fx` says how it was obtained (`declared`, `rate`,
   `set_on`). Project and organisation reports, and the CSV, all go through the same function.
5. **Every surface says it is converted.**
   - The CSV keeps its columns and their order, appends `cost_<code>`, and ends with a `declared_rate` row and
     a `note` row saying the figure is the USD as measured converted at the organisation's declared rate.
   - The console formats with `Intl.NumberFormat` for the currency (`MX$1,234.50`, never a bare `$` that could
     be read as dollars; the live board, which writes the symbol only, writes the code for any currency but
     the euro and the dollar). The Spend page and its month panel carry a sentence with the rate and the
     day it was set; the Organisation card says the same where the rate is declared. The USD figures stay
     available: they are the other column of every export, and the project, work item and agent screens,
     which already show dollars (S23-10), are unchanged.
   - The CLI prints `MXN 87.50` for a dollar amount in a declared currency, using the organisation's rate
     (not the ledger's euro ratio); without a declared rate an amount keeps its own symbol, as before.
6. **Budgets and ceilings stay in USD.** They are set (`daily_project_usd`, `budget_usd`, the organisation's
   ceilings), enforced and alerted in USD, against the USD ledger: a rate someone declared must never move
   when a run is stopped. `CostTotal.budget_usd` stays USD. Where the Spend page compares the month with the
   budget it shows the budget converted at the declared rate, and the page's note says that budgets stay in USD.

## Consequences

- A rate is a statement by the organisation, not a market price: nothing refreshes it. It carries its date so
  that a reader can see how old the conversion is; refreshing it is a `PATCH`. The platform does not warn
  when it is old (not done).
- The same ledger line shown in two organisations that declared different rates differs: each organisation's
  figure is consistent with its own declaration, and the USD figure is the common ground.
- The `Org.currency` and `CostReport.currency` contract fields stop being enums; a client that listed `EUR` and
  `USD` must accept any three-letter code. The console's and the CLI's own readers do.
- Rejected: converting the ledger at write time into a third column (rewrites history when the rate changes,
  and one column per currency); converting in the console only (the CSV and the CLI would disagree); a rate
  feed (a vendor and a freshness policy no story asked for); converting budgets (a declared number would
  change what is enforced).

## Not done

- Refreshing, expiring or warning on an old declared rate; keeping a history of declared rates.
- The mock API of the demo console does not convert (`cost_display` is absent from its reports).
- Per-project display currencies; only the organisation chooses.
