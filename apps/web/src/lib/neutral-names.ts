// SPDX-License-Identifier: Apache-2.0
/** The neutral names of ADR 0181 C1 (their former names left on 2026-11-23), and the C2 readers. */

/** The relation of the link to the change a step proposes for review (`rel: change_request`). */
export const CHANGE_REQUEST = "change_request";

type Linked = { links?: Array<{ rel: string; url: string }> | null };

/** The URL of the change a work item or a step's artifacts propose, if any. */
export function changeRequestOf(item: Linked | null | undefined): string | null {
  if (!item) return null;
  return item.links?.find((link) => link.rel === CHANGE_REQUEST)?.url ?? null;
}

/** The workspace a work item works on, and whether it is fixed. */
export function workspaceOf(item: { workspace?: string | null }): string | null {
  return item.workspace ?? null;
}

export function workspaceFixed(item: { workspace_fixed?: boolean }): boolean {
  return item.workspace_fixed ?? false;
}

/** Whether a catalogue entry needs a workspace its steps read and write. */
export function needsWorkspace(entry: { needs_workspace?: boolean }): boolean {
  return entry.needs_workspace ?? false;
}

/** Whether a step changes the work item's workspace. */
export function writesWorkspace(step: { writes_workspace?: boolean } | null | undefined): boolean {
  return step?.writes_workspace ?? false;
}

/** Whether the change request's body carries an artifact. */
export function inChangeRequest(artifact: { in_change_request?: boolean }): boolean {
  return artifact.in_change_request ?? false;
}

/** The share of work items accepted without a rework loop. */
export function acceptanceRate(
  stats: { first_pass_acceptance_rate?: number | null } | null | undefined,
): number | null | undefined {
  return stats?.first_pass_acceptance_rate;
}

/**
 * A step's proof by name (ADR 0181 C2): the named fact first (`evidence.facts`, what the runner writes
 * beside each field during the window), the field of the same name as a fallback.
 */
export function evidenceValue(
  evidence: Record<string, unknown> | null | undefined,
  name: string,
): string | number | boolean | null {
  if (!evidence) return null;
  const facts = (evidence.facts ?? {}) as Record<string, unknown>;
  const found = name in facts ? facts[name] : evidence[name];
  return typeof found === "string" || typeof found === "number" || typeof found === "boolean" ? found : null;
}

type Delivered = { branch?: string | null; commits?: string[] | null; delivery?: { branch?: string | null; commits?: string[] | null } | null };

/** A step's branch: its `delivery` section first, the former field as a fallback (ADR 0181 C2). */
export function branchOf(artifacts: Delivered | null | undefined): string | null {
  return artifacts?.delivery?.branch ?? artifacts?.branch ?? null;
}

/** A step's commits: its `delivery` section first, the former field as a fallback. */
export function commitsOf(artifacts: Delivered | null | undefined): string[] {
  const section = artifacts?.delivery?.commits;
  return section && section.length > 0 ? section : (artifacts?.commits ?? []);
}
