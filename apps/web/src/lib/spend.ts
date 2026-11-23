// SPDX-License-Identifier: Apache-2.0
/**
 * What the spend screen computes (S29-03): which currency a row is read in, the month's forecast,
 * and the top of a breakdown. Pure, so the numbers on the screen can be checked without a browser.
 */
import type { CostReport } from "./types";

/**
 * The currency an organisation reads its spend in: an ISO 4217 code (ADR 0214). `EUR` and `USD` come from the
 * ledger's own columns; any other code is USD converted at the rate the organisation declared.
 */
export type Currency = string;
type Row = Pick<CostReport["rows"][number], "key" | "cost_usd" | "cost_eur" | "cost_display">;

/** The organisation's display currency (`EUR` when the session does not know it yet). */
export function orgCurrency(orgs: ReadonlyArray<{ slug: string; currency?: string }>, slug: string): Currency {
  return orgs.find((o) => o.slug === slug)?.currency ?? "EUR";
}

/** A row in `currency`: the ledger's column, or the API's conversion at the declared rate (`cost_display`). */
export function amount(row: { cost_usd?: number; cost_eur?: number; cost_display?: number }, currency: Currency): number {
  if (currency === "USD") return row.cost_usd ?? 0;
  if (currency === "EUR") return row.cost_eur ?? 0;
  return row.cost_display ?? 0;
}

/**
 * An amount in `currency`, with the currency's own symbol or code (`€1.00`, `US$1.00`, `MX$1.00`), never a bare
 * `$` that could be read as dollars. An unknown code is shown as `CODE 1.00` rather than thrown.
 */
export function money(value: number | null | undefined, currency: Currency): string {
  if (value === null || value === undefined) return "—";
  try {
    return new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(value);
  } catch {
    return `${currency} ${value.toFixed(2)}`;
  }
}

/**
 * What the report says about how its `cost_display` was obtained: the declared rate and the day it was set,
 * or `null` when the figures are the ledger's own (EUR, USD).
 */
export function declaredRate(
  report: { fx?: { declared: boolean; rate?: number | null; set_on?: string | null } } | undefined,
): { rate: number; setOn: string | null } | null {
  const fx = report?.fx;
  return fx?.declared && fx.rate ? { rate: fx.rate, setOn: fx.set_on ?? null } : null;
}

/** The sentence every converted figure carries (ADR 0214): what it is, at which rate, and that it is declared. */
export function fxNote(currency: Currency, rate: number, setOn: string | null): string {
  const when = setOn ? `, set on ${setOn}` : "";
  return `Amounts are USD as measured, converted to ${currency} at the organisation's declared rate (1 USD = ${rate} ${currency}${when}). The ledger and the budgets stay in USD.`;
}

/** The first day of `now`'s month, UTC, as `YYYY-MM-DD` — the ledger counts days in UTC. */
export function monthStart(now: Date): string {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString().slice(0, 10);
}

export function daysInMonth(now: Date): number {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate();
}

export type MonthForecast = {
  spent: number;
  /** Linear: what has been spent per elapsed day, carried to the month's last day. */
  forecast: number;
  /** The daily budget times the month's days, in the display currency; `null` when there is none. */
  budget: number | null;
  daysElapsed: number;
  daysInMonth: number;
  /** The forecast crosses the budget — or the spend already has. */
  over: boolean;
  /** Share of the budget already spent, 0..1+. */
  ratio: number | null;
};

/**
 * Budget against actual, with a linear forecast to month end. `dayRows` are the per-day rows from
 * the first of the month; the day in progress counts as a day, so the forecast is never made
 * from zero elapsed days. `dailyBudgetUsd` is the report's `total.budget_usd`, converted with
 * `usdTo` (the report's own EUR/USD ratio) when the display currency is not dollars.
 */
export function monthForecast(
  dayRows: Row[],
  now: Date,
  currency: Currency,
  dailyBudgetUsd: number | null | undefined,
  usdTo: number | null,
): MonthForecast {
  const spent = dayRows.reduce((sum, row) => sum + amount(row, currency), 0);
  const days = daysInMonth(now);
  const elapsed = Math.max(1, now.getUTCDate());
  const forecast = (spent / elapsed) * days;
  const rate = currency === "USD" ? 1 : usdTo;
  const budget = dailyBudgetUsd && rate !== null ? dailyBudgetUsd * rate * days : null;
  return {
    spent,
    forecast,
    budget,
    daysElapsed: elapsed,
    daysInMonth: days,
    over: budget !== null && Math.max(spent, forecast) > budget,
    ratio: budget ? spent / budget : null,
  };
}

/** The EUR per USD the ledger applied over a report, or `null` when nothing was spent. */
export function usdToCurrency(total: Pick<CostReport["total"], "cost_usd" | "cost_eur">): number | null {
  const usd = total.cost_usd ?? 0;
  return usd > 0 ? (total.cost_eur ?? 0) / usd : null;
}

/**
 * The units of the display currency per USD for a whole report (ADR 0214): the organisation's declared rate
 * when the report carries one, otherwise the ledger's own EUR ratio (`usdToCurrency`).
 */
export function reportRate(report: {
  total: Pick<CostReport["total"], "cost_usd" | "cost_eur">;
  fx?: { declared: boolean; rate?: number | null };
}): number | null {
  return declaredRate(report)?.rate ?? usdToCurrency(report.total);
}

/** The `n` most expensive rows, with each one's share of the total. */
export function top(rows: Row[], currency: Currency, n = 5): Array<{ key: string; value: number; share: number }> {
  const total = rows.reduce((sum, row) => sum + amount(row, currency), 0);
  return [...rows]
    .sort((a, b) => amount(b, currency) - amount(a, currency))
    .slice(0, n)
    .map((row) => ({ key: row.key, value: amount(row, currency), share: total > 0 ? amount(row, currency) / total : 0 }));
}

/** What a breakdown measures (ADR 0141 §1): the money, or the tokens read and written. */
export type Measure = "cost" | "tokens";

type TokenRow = Pick<CostReport["rows"][number], "key" | "tokens_in" | "tokens_out" | "tokens_cached" | "tokens_cache_write">;

/**
 * The tokens a report's totals read: input, output, and the two parts of the input that went through the
 * provider's cache: the ones read from it (`cached`) and the ones written to it (`cacheWrite`, dearer, ADR 0188).
 */
export type TokenTotals = { input: number; output: number; cached: number; cacheWrite: number };

export function tokenTotals(
  total: Pick<CostReport["total"], "tokens_in" | "tokens_out" | "tokens_cached" | "tokens_cache_write"> | undefined,
): TokenTotals {
  return {
    input: total?.tokens_in ?? 0,
    output: total?.tokens_out ?? 0,
    cached: total?.tokens_cached ?? 0,
    cacheWrite: total?.tokens_cache_write ?? 0,
  };
}

/** A count of tokens, in full with thousands separators: totals are compared, not rounded away. */
export function tokenCount(value: number | null | undefined): string {
  return new Intl.NumberFormat("en-GB").format(value ?? 0);
}

/**
 * The `n` rows that moved the most tokens (input plus output), each with its share of the report's
 * tokens and its own counts. The cached tokens, read and written, are part of the input: they are shown,
 * never added a second time.
 */
export function topTokens(
  rows: TokenRow[],
  n = 5,
): Array<{ key: string; value: number; share: number; input: number; output: number; cached: number; cacheWrite: number }> {
  const moved = (row: TokenRow) => (row.tokens_in ?? 0) + (row.tokens_out ?? 0);
  const total = rows.reduce((sum, row) => sum + moved(row), 0);
  return [...rows]
    .sort((a, b) => moved(b) - moved(a))
    .slice(0, n)
    .map((row) => ({
      key: row.key,
      value: moved(row),
      share: total > 0 ? moved(row) / total : 0,
      input: row.tokens_in ?? 0,
      output: row.tokens_out ?? 0,
      cached: row.tokens_cached ?? 0,
      cacheWrite: row.tokens_cache_write ?? 0,
    }));
}
