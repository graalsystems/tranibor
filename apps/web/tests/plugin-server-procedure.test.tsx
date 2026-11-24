import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import OrgConnectorsPage from "@/app/admin/connectors/page";
import { api } from "@/lib/api";
import type { ConnectorType } from "@/lib/types";
import { serverConnectorOf } from "@/mocks/plugins";
import { proceduresApi } from "@/mocks/procedures";

/**
 * S84-06 (ADR 0215): the connector of a plugin's server is an `mcp/mcp` connector, and its settings show
 * its plugin's own setup procedure (`connector: mcp/servicenow`), not the generic MCP server one.
 */
vi.mock("next/navigation", () => ({ usePathname: () => "/admin/connectors", useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/session", () => ({ useSession: () => ({ org: "acme" }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  const { serverConnectorOf: server } = await import("@/mocks/plugins");
  const { proceduresApi: procedures } = await import("@/mocks/procedures");
  return {
    ...real,
    api: {
      ...real.api,
      orgConnectors: vi.fn().mockResolvedValue({
        items: [server("servicenow", "0.1.0", "servicenow")],
        meta: { has_more: false, next_cursor: null },
      }),
      projects: vi.fn().mockResolvedValue({ items: [], meta: { has_more: false } }),
      connectors: vi.fn().mockResolvedValue([]),
      connectorTypes: vi.fn().mockResolvedValue([
        { kind: "mcp", type: "mcp", display: "MCP server", config_schema: {}, secret_fields: [] } as unknown as ConnectorType,
      ]),
      listProcedures: vi.fn(async (query: Record<string, string | undefined> = {}) => {
        const params = new URLSearchParams(Object.entries(query).filter(([, v]) => v) as [string, string][]);
        return procedures("GET", `/procedures?${params}`);
      }),
      getProcedure: vi.fn(async (id: string) => procedures("GET", `/procedures/${id}`)),
    },
  };
});

describe("a plugin server's connector (S84-06)", () => {
  it("shows its plugin's setup procedure", async () => {
    expect(serverConnectorOf("servicenow", "0.1.0", "servicenow").server?.plugin).toBe("servicenow");
    expect(proceduresApi("GET", "/procedures/connector.mcp.servicenow")).toBeTruthy();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <OrgConnectorsPage />
      </QueryClientProvider>,
    );
    const card = await screen.findByTestId("connector-servicenow");
    fireEvent.click(await within(card).findByRole("button", { name: "How to set this up" }));
    const panel = screen.getByTestId("procedure-panel");
    expect(within(panel).getByRole("heading", { level: 2 })).toHaveTextContent("Connect a ServiceNow instance");
    expect(vi.mocked(api.listProcedures).mock.calls[0]![0]).toMatchObject({ for: "connector:mcp/servicenow" });
  });
});
