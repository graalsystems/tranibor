import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProfilePage from "@/app/library/plugins/profile/page";
import { implementedBy, profileHref, profilesOf } from "@/lib/plugins";
import type { PluginProfile, PluginServerInstance } from "@/lib/types";

/** S86-05: a profile's page lists what implements it and the instances of each, with their state. */
const { api, params } = vi.hoisted(() => ({ api: { pluginProfiles: vi.fn() }, params: { name: "" } }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/library/plugins/profile",
  useRouter: () => ({ push: vi.fn() }),
  useSearchParams: () => new URLSearchParams({ name: params.name }),
}));
vi.mock("@/lib/session", () => ({ useSession: () => ({ org: "acme", me: null }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return { ...real, api: { ...real.api, ...api } };
});

const instance = (name: string, state: PluginServerInstance["state"], server = "shelf"): PluginServerInstance => ({
  name,
  server,
  default: name === server,
  state,
  status: state,
  last_error: null,
});
const NOTES: PluginProfile = {
  name: "notes",
  plugin: "notes",
  profile: "notes/notes@1",
  version: 1,
  description: "A shelf of short notes, whatever the product that keeps them.",
  tools: [
    { name: "list_notes", access: "read", description: null },
    { name: "add_note", access: "write", description: "Adds one." },
  ],
  implementations: [
    { plugin: "shelf", server: "shelf", instances: [instance("shelf", "ready"), instance("shelf-archive", "unconfigured")] },
    { plugin: "wiki", server: "wiki", instances: [instance("wiki", "failing", "wiki")] },
  ],
};
const LONELY: PluginProfile = { ...NOTES, name: "alone", profile: "notes/alone@1", implementations: [] };

beforeEach(() => {
  api.pluginProfiles.mockReset();
  api.pluginProfiles.mockResolvedValue({ items: [NOTES, LONELY] });
});

function page(name: string) {
  params.name = name;
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ProfilePage />
    </QueryClientProvider>,
  );
}

describe("the profile helpers", () => {
  it("find a plugin's profiles and the profiles a server implements, and write a page's address", () => {
    expect(profilesOf([NOTES, LONELY], "notes")).toHaveLength(2);
    expect(profilesOf([NOTES], "shelf")).toEqual([]);
    expect(implementedBy([NOTES, LONELY], "shelf", "shelf")).toEqual(["notes/notes@1"]);
    expect(implementedBy([NOTES], "shelf", "other")).toEqual([]);
    expect(profileHref("notes/notes@1")).toBe("/library/plugins/profile?name=notes%2Fnotes%401");
  });
});

describe("Library → Plugins → a profile", () => {
  it("lists its tools, its implementations and their instances with their state", async () => {
    page("notes/notes@1");
    const shelf = await screen.findByTestId("implementation-shelf-shelf");
    expect(within(shelf).getByTestId("instance-shelf")).toHaveTextContent("ready");
    expect(within(shelf).getByTestId("instance-shelf")).toHaveTextContent("Default");
    expect(within(shelf).getByTestId("instance-shelf-archive")).toHaveTextContent("unconfigured");
    expect(within(shelf).getAllByRole("link", { name: "Connector" }).map((a) => a.getAttribute("href"))).toEqual([
      "/admin/connectors?connector=shelf",
      "/admin/connectors?connector=shelf-archive",
    ]);
    expect(within(screen.getByTestId("implementation-wiki-wiki")).getByTestId("instance-wiki")).toHaveTextContent("failing");
    expect(screen.getByTestId("profile-tools")).toHaveTextContent("list_notes");
    expect(screen.getByTestId("profile-tools")).toHaveTextContent("add_note");
    expect(screen.getByRole("heading", { name: "Implementations (2)" })).toBeInTheDocument();
  });

  it("says plainly that nothing implements a profile", async () => {
    page("notes/alone@1");
    expect(await screen.findByText("Nothing implements it")).toBeInTheDocument();
  });

  it("says no installed plugin declares an unknown profile", async () => {
    page("ghost/none@1");
    expect(await screen.findByText("No such profile")).toBeInTheDocument();
  });
});
