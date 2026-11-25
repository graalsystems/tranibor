import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InboxView } from "@/components/inbox-view";

vi.mock("@/lib/session", () => ({ useSession: () => ({ org: "acme" }) }));
const workItems = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...real,
    api: {
      ...real.api,
      projects: vi.fn().mockResolvedValue({
        items: [{ slug: "billing-api" }, { slug: "checkout-web" }],
        meta: { has_more: false },
      }),
      workItems,
      orgActions: vi.fn().mockResolvedValue({ items: [
        { id: "a1", title: "Open a billing account", project_slug: "billing-api", status: "pending_approval", proposed_by: "agent:x" },
        { id: "a2", title: "Order a checkout laptop", project_slug: "checkout-web", status: "pending_approval", proposed_by: "agent:x" },
      ], meta: { has_more: false, next_cursor: null } }),
      budgetAlerts: vi.fn().mockResolvedValue({ items: [], meta: { has_more: false, next_cursor: null } }),
      orgEgress: vi.fn().mockResolvedValue({ mode: "enforce", ceiling: {}, rules: [], requests: [], can_decide: false }),
    },
  };
});

function view(project?: string) {
  workItems.mockResolvedValue({ items: [], meta: { has_more: false } });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <InboxView project={project} heading="h2" />
    </QueryClientProvider>,
  );
}

describe("the project's inbox (S39-04)", () => {
  it("lists the project's action and not another project's, and links to the organisation's inbox", async () => {
    view("billing-api");
    expect(await screen.findByText("Open a billing account")).toBeInTheDocument();
    expect(screen.queryByText("Order a checkout laptop")).toBeNull();
    expect(screen.getByRole("link", { name: /whole organisation/ })).toHaveAttribute("href", "/inbox");
  });

  it("reads the tickets of that project only", async () => {
    workItems.mockClear();
    view("billing-api");
    await screen.findByText("Open a billing account");
    expect(workItems.mock.calls.map((c) => c[0])).toEqual(["billing-api"]);
  });

  it("without a project, lists everything and has no link to itself", async () => {
    view();
    expect(await screen.findByText("Order a checkout laptop")).toBeInTheDocument();
    expect(screen.getByText("Open a billing account")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /whole organisation/ })).toBeNull();
  });
});

describe("an imported request in the inbox (ADR 0216)", () => {
  it("is listed with the badge, and says what deciding it does", async () => {
    workItems.mockResolvedValue({
      items: [
        {
          id: "w9",
          project_slug: "billing-api",
          tracker_key: "demo:inv-150",
          title: "Localised reminders",
          state: "in_progress",
          imported: true,
          pending_request: {
            id: "r9",
            work_item_id: "w9",
            kind: "approval",
            payload: { summary: "Approve the specification?" },
            requested_at: "2026-11-05T09:40:00Z",
          },
        },
      ],
      meta: { has_more: false },
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <InboxView project="billing-api" heading="h2" />
      </QueryClientProvider>,
    );
    const row = await screen.findByTestId("waiting-w9");
    expect(row).toHaveTextContent("Imported");
    expect(screen.getByTestId("imported-decision-w9")).toHaveTextContent("nothing follows from it");
    expect(screen.getByTestId("imported-badge")).toHaveAttribute("title", expect.stringContaining("demonstration history"));
  });
});
