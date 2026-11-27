import { expect, test, type Page } from "@playwright/test";

/**
 * A demonstration history imported through the API (ADR 0216) shows as imported in every page of the
 * console that lists its rows: the board, the work item and its runs, a run's page, the findings and the
 * inbox's waiting request. The API's tests prove each row carries `imported`; this one proves the console
 * reads it, against the real API (`tools/e2e_live_api.py` opens the import, outside production).
 */
const ORG = "/api/v1/orgs/northwind/projects";
// A fresh project per run: a reused API keeps the previous run's.
const SLUG = `imported-${Date.now()}`;

const INVOICES = `
apiVersion: tranibor/v1
kind: Workflow
metadata:
  name: invoices
  version: 1
initial: inbox
actors:
  implementer: { type: agent, role: implement }
  verifier: { type: agent, role: verify }
states:
  inbox: { display: Inbox }
  in_progress: { display: In progress }
  shipped: { display: Shipped, terminal: true }
transitions:
  - { id: t-implement, from: inbox, to: in_progress, by: implementer }
  - { id: t-shipped, from: in_progress, to: shipped, by: verifier }
`;

const HISTORY = {
  schema: "tranibor/DemoHistory/v1",
  workflow: "invoices",
  work_items: [
    {
      id: "inv-142",
      title: "Credit notes are not deducted from the invoice total",
      created_at: "2026-11-02T09:00:00Z",
      history: [
        { state: "inbox", at: "2026-11-02T09:00:00Z" },
        { state: "shipped", at: "2026-11-03T08:00:00Z" },
      ],
      runs: [
        {
          id: "implement-1",
          stage: "implement",
          status: "succeeded",
          started_at: "2026-11-02T10:31:00Z",
          duration_s: 600,
          cost_usd: 0.5,
        },
      ],
      findings: [
        {
          id: "reset-link",
          title: "The invoice download link does not expire",
          type: "security",
          severity: "medium",
          evidence: "src/invoices/links.py:41",
        },
      ],
    },
    {
      id: "inv-150",
      title: "Send the overdue invoice reminders in the customer's language",
      created_at: "2026-11-05T09:00:00Z",
      history: [
        { state: "inbox", at: "2026-11-05T09:00:00Z" },
        { state: "in_progress", at: "2026-11-05T09:30:00Z" },
      ],
      pending: {
        id: "spec-review",
        kind: "approval",
        summary: "Approve the specification of the localised reminders?",
        requested_at: "2026-11-05T09:40:00Z",
      },
    },
  ],
};

type Item = { id: string; tracker_key: string; imported: boolean };

async function imported(
  page: Page,
): Promise<{ project: string; shipped: Item; waiting: Item; run: string }> {
  const created = await page.request.post(ORG, {
    data: {
      slug: SLUG,
      name: "Imported history",
      config: {
        slug: SLUG,
        org: "northwind",
        delivery: {
          repo: {
            url: "https://github.com/northwind/billing-api.git",
            default_branch: "main",
          },
        },
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const project = (await created.json()).id as string;
  const published = await page.request.put(
    `/api/v1/projects/${project}/workflows/invoices`,
    {
      data: { yaml: INVOICES },
    },
  );
  expect([200, 201], await published.text()).toContain(published.status());
  const answer = await page.request.post(
    `/api/v1/projects/${project}/demo-history`,
    { data: HISTORY },
  );
  expect(answer.status(), await answer.text()).toBe(200);

  const items: Item[] = (
    await (
      await page.request.get(`/api/v1/projects/${project}/work-items`)
    ).json()
  ).items;
  const byKey = (key: string): Item => {
    const found = items.find((item) => item.tracker_key === key);
    expect(found, `${key} imported`).toBeDefined();
    return found as Item;
  };
  const shipped = byKey("demo:inv-142");
  const waiting = byKey("demo:inv-150");
  const runs = (
    await (
      await page.request.get(`/api/v1/work-items/${shipped.id}/runs`)
    ).json()
  ).items;
  return { project, shipped, waiting, run: runs[0].id as string };
}

test("an imported history is marked imported on every page that shows it", async ({
  page,
}) => {
  await page.goto("/api/v1/auth/login?as=admin@northwind.dev");
  const { shipped, waiting, run } = await imported(page);
  const badge = page.getByTestId("imported-badge");

  await page.goto(`/p/${SLUG}/board`);
  await expect(page.getByText("demo:inv-150")).toBeVisible();
  await expect(badge.first()).toBeVisible();
  await expect(badge.first()).toHaveText("Imported");

  await page.goto(`/p/${SLUG}/items/${shipped.id}`);
  await expect(
    page
      .getByText("Credit notes are not deducted from the invoice total")
      .first(),
  ).toBeVisible();
  // The work item's own badge, and its run's in the list of runs.
  await expect.poll(async () => badge.count()).toBeGreaterThanOrEqual(2);

  await page.goto(`/p/${SLUG}/runs/${run}`);
  await expect(badge.first()).toBeVisible();

  await page.goto(`/p/${SLUG}/findings`);
  await expect(
    page.getByText("The invoice download link does not expire"),
  ).toBeVisible();
  await expect(badge.first()).toBeVisible();

  await page.goto("/inbox");
  const request = page.getByTestId(`waiting-${waiting.id}`);
  await expect(request).toBeVisible();
  await expect(request.getByTestId("imported-badge")).toBeVisible();
  await expect(
    page.getByTestId(`imported-decision-${waiting.id}`),
  ).toBeVisible();
});
