import { expect, test } from "@playwright/test";

/**
 * ServiceNow is set up in the console like Jira (ADR 0228), against the real API: the new-project page asks the
 * chosen tracker's settings and secrets, creates the project with them, and the API keeps a `servicenow`
 * tracker connector with its configuration and its sealed secrets (never their values).
 */
const SLUG = `service-desk-${Date.now()}`;

type Connector = {
  kind: string;
  type: string;
  config: Record<string, unknown>;
  secrets: { field: string }[];
};

test("a project is created with a ServiceNow tracker, set up from the new-project page", async ({
  page,
}) => {
  await page.goto("/api/v1/auth/login?as=admin@northwind.dev");
  await page.goto("/projects/new");

  await page.getByLabel(/no code repository/).check();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByLabel("Identifier (slug)").fill(SLUG);
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page
    .getByLabel("Tracker (where tickets come from)")
    .selectOption("servicenow");

  const settings = page.getByTestId("tracker-settings");
  await expect(settings).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Next", exact: true }),
  ).toBeDisabled();
  await settings
    .getByLabel(/instance url/)
    .fill("https://acme.service-now.com");
  await settings.getByLabel(/client id/).fill("tranibor-integration");
  await settings.getByLabel(/assignment groups/).fill("Service Desk");
  await settings
    .getByLabel("client secret value")
    .fill("an-oauth-client-secret");
  await settings
    .getByLabel("webhook secret value")
    .fill("a-shared-webhook-secret");
  await settings.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("tracker-settings-kept")).toBeVisible();

  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("button", { name: "Create" }).click();
  await page.waitForURL(`**/p/${SLUG}`);

  const answer = await page.request.get(
    `/api/v1/projects/northwind:${SLUG}/connectors`,
  );
  expect(answer.status(), await answer.text()).toBe(200);
  const listed = await answer.json();
  const connectors: Connector[] = Array.isArray(listed) ? listed : listed.items;
  const tracker = connectors.find((c) => c.kind === "tracker");
  expect(tracker?.type).toBe("servicenow");
  expect(tracker?.config).toMatchObject({
    instance_url: "https://acme.service-now.com",
    client_id: "tranibor-integration",
    assignment_groups: ["Service Desk"],
  });
  expect((tracker?.secrets ?? []).map((s) => s.field).sort()).toEqual([
    "client_secret",
    "webhook_secret",
  ]);
  expect(JSON.stringify(tracker)).not.toContain("an-oauth-client-secret");
});
