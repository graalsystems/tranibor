// SPDX-License-Identifier: Apache-2.0
/**
 * The setup procedures (ADR 0067), as the console uses them: what to look up for a form, and what
 * it remembers about who has already been shown one. The text itself is served by the API, with
 * this deployment's URLs filled in; nothing here is a vendor fact.
 */

import type { Procedure } from "@/lib/types";

export type { Procedure };

const SEEN_PREFIX = "choregos.procedure.seen.";

/** `connector:documents/sharepoint`: what `GET /procedures?for=` looks a connector type up by. */
export function connectorTarget(kind: string, type: string): string {
  return `connector:${kind}/${type}`;
}

/** The installer's mark on a plugin server's connector: `plugin:<plugin>/<server>@<version>`. */
const PLUGIN_ENTRY = /^plugin:([^/@]+)\/([^/@]+)@/;

/**
 * What a connector's setup panel looks up, in order (ADR 0215). A plugin's server is an `mcp/mcp`
 * connector: its plugin's own procedure applies to `mcp/<plugin>` or `mcp/<server>`, and the generic MCP
 * server procedure stands in when the plugin ships none. Any other connector asks its own type.
 */
export function connectorTargets(connector: {
  kind: string;
  type: string;
  server?: { plugin: string; server: string } | null;
  catalogue_entry?: string | null;
}): string[] {
  const own = connectorTarget(connector.kind, connector.type);
  const entry = PLUGIN_ENTRY.exec(connector.catalogue_entry ?? "");
  const plugin = connector.server?.plugin ?? entry?.[1];
  const server = connector.server?.server ?? entry?.[2];
  if (!plugin || connector.kind !== "mcp") return [own];
  const candidates = [connectorTarget("mcp", plugin), ...(server ? [connectorTarget("mcp", server)] : []), own];
  return [...new Set(candidates)];
}

export function sourceTarget(kind: string): string {
  return `source:${kind}`;
}

/** The catalogue preset an MCP server's address belongs to, when it is a known one. */
export function presetOfMcpUrl(url: unknown): string | undefined {
  const host = hostOf(url);
  return host === "mcp.atlassian.com" ? "atlassian-rovo" : undefined;
}

/** The host of an address, for a lookup by agent-card host; `undefined` for anything that is not one. */
export function hostOf(url: unknown): string | undefined {
  if (typeof url !== "string" || !url.trim()) return undefined;
  try {
    return new URL(url.trim()).hostname.toLowerCase();
  } catch {
    return undefined;
  }
}

/** Has this person been shown this procedure on its own? Per user and per id, kept in the browser only. */
export function wasSeen(id: string): boolean {
  try {
    return window.localStorage.getItem(SEEN_PREFIX + id) === "1";
  } catch {
    return false;
  }
}

export function markSeen(id: string): void {
  try {
    window.localStorage.setItem(SEEN_PREFIX + id, "1");
  } catch {
    // Private window or blocked storage: the panel simply opens again next time.
  }
}

/** The only links a procedure's text may carry: its own vendor documents. */
export function allowedLinks(procedure: Procedure): string[] {
  return procedure.vendor_docs.map((doc) => doc.url);
}

const VALUE_LABELS: Record<string, string> = {
  api_url: "This platform's API address",
  public_url: "This platform's console address",
  org: "Your organisation",
  "oauth_callback:remote": "OAuth callback URL to register at the vendor",
};

/** What a copy box is called: `webhook_url:jira` becomes "Webhook URL for jira". */
export function labelOfValue(key: string): string {
  const known = VALUE_LABELS[key];
  if (known) return known;
  const [name, argument] = key.split(":");
  if (name === "webhook_url" && argument) return `Webhook URL for ${argument}`;
  return key;
}

export const AUDIENCE: Record<Procedure["audience"], string> = {
  org_admin: "An organisation administrator",
  project_owner: "A project owner",
  operator: "Whoever operates the platform",
  everyone: "Anyone",
};
