import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { Suspense } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FindingsPage from "@/app/p/[slug]/findings/page";
import { api } from "@/lib/api";
import { findingTypeLabel } from "@/lib/finding-type";

/** A workflow's own finding types (ADR 0217): the console shows the title the workflow declares. */
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, findings: vi.fn(), findingAction: vi.fn() } };
});

const base = {
  project_slug: "warranty",
  severity: "high",
  evidence: "claims/2026-03-1187",
  status: "pending",
  occurrences: 1,
  created_at: "2026-11-24T10:00:00Z",
};

describe("a finding's type label", () => {
  it("is the workflow's title when it declares one, else the name", () => {
    expect(findingTypeLabel({ type: "duplicate-claim", type_title: "Suspected duplicate claim" })).toBe(
      "Suspected duplicate claim",
    );
    expect(findingTypeLabel({ type: "security", type_title: null })).toBe("security");
    expect(findingTypeLabel({ type: "docs" })).toBe("docs");
    expect(findingTypeLabel({ type: "ux", type_title: "  " })).toBe("ux");
  });
});

describe("the findings page", () => {
  beforeEach(() => {
    vi.mocked(api.findings).mockResolvedValue({
      items: [
        { ...base, id: "f1", title: "Serial 4471 was claimed in March", type: "duplicate-claim", type_title: "Suspected duplicate claim" },
        { ...base, id: "f2", title: "Password in a fixture", type: "security", type_title: null },
      ],
      meta: { has_more: false },
    } as never);
  });

  it("shows the declared title of a workflow's type and the name of a core one", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Suspense fallback={null}>
          <FindingsPage params={Promise.resolve({ slug: "warranty" })} />
        </Suspense>
      </QueryClientProvider>,
    );
    const title = await waitFor(() => screen.getByText("Suspected duplicate claim"));
    expect(title.getAttribute("title")).toBe("duplicate-claim");
    expect(screen.getByText("security")).toBeTruthy();
    expect(screen.queryByText("duplicate-claim")).toBeNull();
  });
});
