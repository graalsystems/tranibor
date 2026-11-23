import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import SpendPage from "@/app/costs/page";
import { liveMoney } from "@/components/live/live-kit";
import { api } from "@/lib/api";
import { amount, declaredRate, fxNote, money, orgCurrency, reportRate, usdToCurrency } from "@/lib/spend";
import type { CostReport } from "@/lib/types";

/**
 * An organisation may read its spend in any ISO 4217 currency, at the rate it declares (ADR 0213): the
 * ledger stays in USD, the figure shown is labelled with its currency and says it is converted.
 */
const fx = { declared: true, rate: 17.5, set_on: "2026-11-20" };
const line = { cost_usd: 2, cost_eur: 1.84, cost_display: 35 };

describe("the money formatter", () => {
  it("writes each currency with its own mark, never a bare dollar sign for pesos", () => {
    expect(money(1234.5, "EUR")).toBe("€1,234.50");
    expect(money(1234.5, "USD")).toBe("US$1,234.50");
    expect(money(1234.5, "MXN")).toBe("MX$1,234.50");
    expect(money(1234.5, "MXN")).not.toBe(money(1234.5, "USD"));
    expect(money(5, "COP")).toContain("COP");
  });

  it("a currency with no minor unit has no decimals, and an unknown code is shown with its code, not thrown", () => {
    expect(money(1500, "JPY")).toBe("JP¥1,500");
    expect(money(12.5, "ZZ")).toBe("ZZ 12.50");
    expect(money(null, "MXN")).toBe("—");
    expect(money(undefined, "MXN")).toBe("—");
  });

  it("reads the live board's amounts the same way: a code, not a symbol that could be dollars", () => {
    expect(liveMoney(2, "MXN", 17.5)).toBe("MXN\u00a035.00");
    expect(liveMoney(2, "EUR", 0.92)).toBe("€1.84");
    expect(liveMoney(2, "USD", null)).toBe("$2.00");
    expect(liveMoney(2, "MXN", null)).toBe("$2.00"); // no rate: told in dollars, never converted by a guess
    expect(liveMoney(0.004, "MXN", 17.5)).toBe("MXN\u00a00.07");
  });
});

describe("which figure a row is read in", () => {
  it("takes the ledger's own column for EUR and USD, and the API's conversion for a declared currency", () => {
    expect(amount(line, "USD")).toBe(2);
    expect(amount(line, "EUR")).toBe(1.84);
    expect(amount(line, "MXN")).toBe(35);
    expect(amount({ cost_usd: 2, cost_eur: 1.84 }, "MXN")).toBe(0);
  });

  it("uses the declared rate when the report carries one, the ledger's ratio otherwise", () => {
    const total = { cost_usd: 100, cost_eur: 92 };
    expect(reportRate({ total, fx })).toBe(17.5);
    expect(reportRate({ total: { cost_usd: 0, cost_eur: 0 }, fx })).toBe(17.5); // known before anything is spent
    expect(reportRate({ total, fx: { declared: false } })).toBe(usdToCurrency(total));
    expect(reportRate({ total: { cost_usd: 0, cost_eur: 0 } })).toBeNull();
  });

  it("finds the organisation's currency, EUR when it is not known yet", () => {
    expect(orgCurrency([{ slug: "acme", currency: "MXN" }], "acme")).toBe("MXN");
    expect(orgCurrency([{ slug: "acme", currency: "MXN" }], "other")).toBe("EUR");
  });

  it("says how the figures were converted, with the rate and the day it was set", () => {
    expect(declaredRate({ fx })).toEqual({ rate: 17.5, setOn: "2026-11-20" });
    expect(declaredRate({ fx: { declared: false, rate: null } })).toBeNull();
    const note = fxNote("MXN", 17.5, "2026-11-20");
    expect(note).toContain("1 USD = 17.5 MXN");
    expect(note).toContain("declared rate");
    expect(note).toContain("2026-11-20");
    expect(note).toContain("budgets stay in USD");
  });
});

const params = { current: new URLSearchParams() };
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => params.current,
  usePathname: () => "/costs",
}));
const session = vi.hoisted(() => ({ currency: "MXN" }));
vi.mock("@/lib/session", () => ({
  useSession: () => ({ org: "acme", orgs: [{ slug: "acme", currency: session.currency }], me: null }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, costs: vi.fn(), orgCosts: vi.fn(), projects: vi.fn() } };
});

function report(group_by: string, declared: boolean): CostReport {
  return {
    group_by,
    currency: declared ? "MXN" : "EUR",
    fx: declared ? fx : { declared: false },
    rows: [{ key: "2026-11-20", ...line, tokens_in: 0, tokens_out: 0, runs: 1 }],
    total: { ...line, budget_usd: 150 },
  } as CostReport;
}

async function renderSpend(declared: boolean) {
  session.currency = declared ? "MXN" : "EUR";
  params.current = new URLSearchParams();
  vi.mocked(api.projects).mockResolvedValue({ items: [] } as never);
  vi.mocked(api.orgCosts).mockImplementation((_org, group) => Promise.resolve(report(group ?? "day", declared)));
  await act(async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SpendPage />
      </QueryClientProvider>,
    );
  });
}

afterEach(() => vi.clearAllMocks());

describe("the Spend page in a declared currency", () => {
  it("shows the converted total labelled with its currency, and says it is converted at the declared rate", async () => {
    await renderSpend(true);
    expect(await screen.findByTestId("period-total")).toHaveProperty("textContent", expect.stringContaining("MX$35.00"));
    const notes = await screen.findAllByTestId("fx-note");
    expect(notes.length).toBeGreaterThan(0);
    expect(within(notes[0]!).getByText(/converted to MXN at the organisation's declared rate \(1 USD = 17\.5 MXN, set on 2026-11-20\)/)).toBeTruthy();
    expect(screen.queryByText(/€/)).toBeNull();
  });

  it("says nothing of conversion for a ledger currency", async () => {
    await renderSpend(false);
    expect(await screen.findByTestId("period-total")).toHaveProperty("textContent", expect.stringContaining("€1.84"));
    expect(screen.queryByTestId("fx-note")).toBeNull();
  });
});
