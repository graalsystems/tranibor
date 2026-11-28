import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import NewProjectPage from "@/app/projects/new/page";
import { api } from "@/lib/api";

const pousser = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pousser }) }));
vi.mock("@/lib/session", () => ({ useSession: () => ({ org: "acme" }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const reel = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...reel,
    api: {
      ...reel.api,
      templates: vi.fn().mockResolvedValue([]),
      connectorTypes: vi.fn().mockResolvedValue([]),
      createProject: vi.fn().mockResolvedValue({ id: "p1", slug: "people-ops" }),
      putConnector: vi
        .fn()
        .mockRejectedValueOnce(new Error("tracker unreachable"))
        .mockResolvedValue({}),
      provision: vi.fn(),
      workflowCatalogue: vi.fn().mockResolvedValue([
        { source: "core", name: "default-request", version: 1, title: "default-request", needs_workspace: false, agents: [] },
        { source: "core", name: "default-simple", version: 1, title: "default-simple", needs_workspace: true, agents: [] },
        { source: "template:joiners-leavers", name: "onboarding", version: 1, title: "Onboarding", needs_workspace: false, agents: [] },
      ]),
      adoptWorkflow: vi.fn().mockResolvedValue({}),
    },
  };
});

function ouvrir() {
  document.body.innerHTML = "";
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NewProjectPage />
    </QueryClientProvider>,
  );
}

/** Up to the summary, without a repository. */
function remplir() {
  fireEvent.click(screen.getByLabelText(/no code repository/));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "people-ops" } });
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
}

describe("the project creation wizard (S23-06)", () => {
  it("says why \"next\" waits, and summarises in words, not in keys", () => {
    ouvrir();
    fireEvent.click(screen.getByLabelText(/no code repository/));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toHaveAccessibleDescription("An identifier is needed to go on");
    fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "people-ops" } });
    expect(screen.queryByText("An identifier is needed to go on")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    const recap = screen.getByTestId("summary");
    expect(recap).toHaveTextContent("Identifier");
    expect(recap).not.toHaveTextContent("sansDepot");
  });

  it("resumes where it failed, without recreating the project", async () => {
    ouvrir();
    remplir();

    // The project is created, the tracker is refused: the error says so, and \"retry\" does not recreate the project.
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText(/people-ops was created, but connecting its tracker failed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "open the project" })).toHaveAttribute("href", "/p/people-ops");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(pousser).toHaveBeenCalledWith("/p/people-ops"));
    expect(api.createProject).toHaveBeenCalledTimes(1);
    expect(api.putConnector).toHaveBeenCalledTimes(2);
  });

  it("without a repository, offers only workflows that need none, and adopts the chosen one as the default", async () => {
    ouvrir();
    fireEvent.click(screen.getByLabelText(/no code repository/));
    const choice = screen.getByLabelText("Workflow");
    await screen.findByRole("option", { name: /Onboarding/ });
    expect(screen.queryByRole("option", { name: /^default-simple/ })).toBeNull();
    fireEvent.change(choice, { target: { value: "template:joiners-leavers/onboarding" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "people-ops" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByTestId("summary")).toHaveTextContent("Onboarding (template:joiners-leavers)");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(api.adoptWorkflow).toHaveBeenCalledWith("p1", {
        source: "template:joiners-leavers",
        name: "onboarding",
        version: 1,
        make_default: true,
      }),
    );
  });

  it("asks what the project delivers first, and asks a business process for no repository, language or Slack channel (owner review of 2026-11-03)", async () => {
    ouvrir();
    expect(screen.getByRole("button", { name: "Next" })).toHaveAccessibleDescription("Say what the project delivers to go on");
    expect(screen.queryByLabelText("Stack template")).toBeNull();
    fireEvent.click(screen.getByLabelText(/^A business process/));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.queryByLabelText("Repository")).toBeNull();
    expect(screen.queryByLabelText("Main language")).toBeNull();
    expect(screen.getByLabelText("Identifier (slug)")).toHaveAttribute("placeholder", "employee-onboarding");
    fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "people-ops" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByLabelText(/Slack channel/)).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const recap = screen.getByTestId("summary");
    expect(recap).not.toHaveTextContent("Slack");
    expect(recap).not.toHaveTextContent("Main language");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalled());
    const body = vi.mocked(api.createProject).mock.calls.at(-1)![1] as { config: Record<string, unknown> };
    expect(body.config).not.toHaveProperty("notify");
    expect(body.config).not.toHaveProperty("repo");
    expect(body.config).not.toHaveProperty("delivery");
  });

  it("writes a software project's repository and GitOps in its delivery section only (ADR 0181 C2)", async () => {
    ouvrir();
    fireEvent.click(screen.getByLabelText(/^Software, from a code repository/));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "billing-api" } });
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "acme/billing-api" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("GitOps repository (environments)"), {
      target: { value: "https://github.com/acme/gitops.git" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalled());
    const body = vi.mocked(api.createProject).mock.calls.at(-1)![1] as { config: Record<string, unknown> };
    expect(body.config.delivery).toEqual({
      repo: { url: "https://github.com/acme/billing-api.git", default_branch: "main", language: "python" },
      gitops: { repo_url: "https://github.com/acme/gitops.git", apps: ["billing-api"] },
    });
    expect(body.config).not.toHaveProperty("repo");
    expect(body.config).not.toHaveProperty("gitops");
  });

  it("a software project is asked for its repository and language", () => {
    ouvrir();
    fireEvent.click(screen.getByLabelText(/^Software, from a code repository/));
    expect(screen.getByText("Stack template")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByLabelText("Repository")).toBeInTheDocument();
    expect(screen.getByText("Main language")).toBeInTheDocument();
  });
});

describe("an external tracker is set up in the wizard, its secrets included (ADR 0228)", () => {
  it("waits for ServiceNow's settings, then writes them and the sealed secret with the project", async () => {
    vi.mocked(api.connectorTypes).mockResolvedValue([
      {
        kind: "tracker",
        type: "servicenow",
        display: "ServiceNow",
        config_schema: {
          type: "object",
          required: ["instance_url"],
          properties: { instance_url: { type: "string", title: "Instance URL" } },
        },
        secret_fields: ["password"],
      },
    ] as never);
    ouvrir();
    fireEvent.click(screen.getByLabelText(/no code repository/));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("Identifier (slug)"), { target: { value: "service-desk" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.change(screen.getByLabelText("Tracker (where tickets come from)"), { target: { value: "servicenow" } });

    const next = await screen.findByRole("button", { name: "Next" });
    await screen.findByTestId("tracker-settings");
    expect(next).toBeDisabled();
    expect(next).toHaveAccessibleDescription("Fill in the tracker's settings and save them to go on");
    fireEvent.change(screen.getByLabelText(/Instance URL/), { target: { value: "https://acme.service-now.com" } });
    fireEvent.change(screen.getByLabelText("password value"), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByTestId("tracker-settings-kept")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(api.putConnector).toHaveBeenLastCalledWith(
        "p1",
        "tracker",
        expect.objectContaining({
          type: "servicenow",
          config: { instance_url: "https://acme.service-now.com" },
          secret_values: { password: "s3cret" },
        }),
      ),
    );
  });
});
