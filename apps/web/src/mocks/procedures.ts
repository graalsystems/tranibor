// SPDX-License-Identifier: Apache-2.0
/**
 * The setup procedures on the mock API: the real files, rendered with example addresses by
 * `tools/procedures/export_docs.py --mocks` (a test refuses a stale copy), then those of the installed
 * plugins, as the API lists them beside the platform's (a shipped id wins). The lookup follows the
 * API's: a preset before the generic procedure.
 */

import { ApiError } from "@/lib/api";
import type { Procedure } from "@/lib/types";
import { installedPlugins } from "./plugins";
import raw from "./procedures.json";

const SHIPPED = raw as unknown as Procedure[];

/**
 * What the demo's plugins ship: the `servicenow` plugin's server procedure, shortened from the plugins'
 * repository, so that its server's connector shows it instead of the generic MCP one (ADR 0215).
 */
const PLUGIN_PROCEDURES: Procedure[] = [
  {
    id: "connector.mcp.servicenow",
    title: "Connect a ServiceNow instance",
    version: 2,
    audience: "org_admin",
    applies_to: { connector: "mcp/servicenow" },
    link: null,
    reviewed: "2026-11-21",
    sections: [
      {
        key: "before",
        title: "Before you start",
        markdown:
          "- A ServiceNow instance you administer; a Personal Developer Instance is enough to try the plugin.\n" +
          "- The `servicenow` plugin installed in your organisation. Its server runs in the platform; there is nothing to host.\n" +
          "- A random secret of at least 32 characters for the webhook.",
      },
      {
        key: "at",
        title: "At ServiceNow",
        markdown:
          "1. **The integration user**: `choregos.integration`, **Web service access only**.\n" +
          "2. **The OAuth inbound integration** (client credentials), its **OAuth Application User** the integration user.\n" +
          "3. **The Business Rule** on **Incident**, after insert and update, posting to the webhook address below.",
      },
      {
        key: "permissions",
        title: "Permissions",
        markdown: "A custom role `choregos_integration` holding table ACLs only: no `admin`, no `itil`.",
      },
      {
        key: "in_choregos",
        title: "In Tranibor",
        markdown: "On this connector, **Edit settings**: `base_url`, `auth`, `client_id` and `client_secret`, `webhook_secret`, `assignment_groups`.",
      },
      {
        key: "test",
        title: "Test it",
        markdown: "Create an incident assigned to one of the groups: it appears on the project's board within a minute.",
      },
      {
        key: "fails",
        title: "When it fails",
        markdown: "- `401` from the instance: wrong client ID or secret, or the client credentials grant is not enabled.",
      },
    ],
    values: {
      "plugin_webhook_url:servicenow/servicenow": "https://choregos.example.com/api/v1/webhooks/plugins/acme/servicenow/servicenow",
    },
    egress: ["<instance>.service-now.com"],
    fields: {
      base_url: "https://<instance>.service-now.com, without a trailing slash",
      client_secret: "its Client Secret, typed once",
    },
    vendor_docs: [
      {
        title: "Table API",
        url: "https://www.servicenow.com/docs/bundle/zurich-api-reference/page/integrate/inbound-rest/concept/c_TableAPI.html",
      },
    ],
    origin: "plugin:servicenow",
  },
];

/** The platform's procedures, then those of the plugins installed now. */
function procedures(): Procedure[] {
  const installed = new Set(installedPlugins.map((p) => `plugin:${p.name}`));
  const shipped = new Set(SHIPPED.map((p) => p.id));
  return [...SHIPPED, ...PLUGIN_PROCEDURES.filter((p) => installed.has(p.origin) && !shipped.has(p.id))];
}

function summary(procedure: Procedure) {
  const { id, title, version, audience, applies_to, link } = procedure;
  return { id, title, version, audience, applies_to, link };
}

function best(target: string, cardHost: string | null, preset: string | null): Procedure | undefined {
  const [key, ...rest] = target.split(":");
  const value = rest.join(":");
  const candidates = procedures().filter((p) => p.applies_to[key!] === value);
  const preferred = candidates.find((p) => {
    const extra = Object.entries(p.applies_to).filter(([k]) => k === "card_host" || k === "preset");
    return (
      extra.length > 0 &&
      extra.every(([k, v]) => (k === "card_host" ? cardHost === v : preset === v))
    );
  });
  return preferred ?? candidates.find((p) => !("card_host" in p.applies_to) && !("preset" in p.applies_to));
}

/** `undefined` when the path is not a procedure route. */
export function proceduresApi(method: string, path: string): unknown {
  if (method !== "GET") return undefined;
  const [route, query = ""] = path.split("?");
  if (route === "/procedures") {
    const params = new URLSearchParams(query);
    const target = params.get("for");
    if (!target) return procedures().map(summary);
    const found = best(target, params.get("card_host"), params.get("preset"));
    return found ? [summary(found)] : [];
  }
  const one = /^\/procedures\/([^/]+)$/.exec(route ?? "");
  if (one) {
    const found = procedures().find((p) => p.id === decodeURIComponent(one[1]!));
    if (!found) throw new ApiError(404, { title: "Not found" });
    return structuredClone(found);
  }
  return undefined;
}
