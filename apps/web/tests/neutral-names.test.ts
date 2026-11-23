// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { creatableScopes } from "@/components/agents/registry";
import { rank, roleLabel } from "@/components/members";
import {
  acceptanceRate,
  changeRequestOf,
  inChangeRequest,
  needsWorkspace,
  workspaceFixed,
  workspaceOf,
  writesWorkspace,
} from "@/lib/neutral-names";
import { neutralRole } from "@/lib/roles";

const PR = "https://github.com/northwind/billing-api/pull/7";

describe("C1's neutral names (ADR 0181), alone since their former names left on 2026-11-23", () => {
  it("reads the neutral roles", () => {
    expect(neutralRole("approver")).toBe("approver");
    expect(roleLabel("approver")).toBe("Approver");
    expect(rank("approver")).toBeGreaterThan(rank("contributor"));
    expect(creatableScopes([{ org: "acme", role: "approver" }], "acme").personal).toBe(true);
    expect(creatableScopes([{ org: "acme", role: "viewer" }], "acme").personal).toBe(false);
  });

  it("reads the change request from the link alone", () => {
    expect(changeRequestOf({ links: [{ rel: "change_request", url: PR }] })).toBe(PR);
    expect(changeRequestOf({ links: [{ rel: "other", url: "x" }] })).toBeNull();
    expect(changeRequestOf(null)).toBeNull();
  });

  it("reads each renamed field under its neutral name alone", () => {
    expect(workspaceOf({ workspace: "a" })).toBe("a");
    expect(workspaceOf({})).toBeNull();
    expect(workspaceFixed({ workspace_fixed: true })).toBe(true);
    expect(needsWorkspace({ needs_workspace: true })).toBe(true);
    expect(writesWorkspace({ writes_workspace: true })).toBe(true);
    expect(writesWorkspace(undefined)).toBe(false);
    expect(inChangeRequest({ in_change_request: true })).toBe(true);
    expect(acceptanceRate({ first_pass_acceptance_rate: 0.25 })).toBe(0.25);
  });
});

describe("C2's delivery sections and named facts (ADR 0181): the former fields are no longer read", () => {
  it("reads a proof as its named fact only", async () => {
    const { evidenceValue } = await import("@/lib/neutral-names");
    expect(evidenceValue({ facts: { tests_run: 7 }, tests_run: 3 }, "tests_run")).toBe(7);
    expect(evidenceValue({ tests_run: 3 }, "tests_run")).toBeNull();
    expect(evidenceValue({ facts: { lint: "ok" } }, "lint")).toBe("ok");
    expect(evidenceValue(undefined, "tests_run")).toBeNull();
    expect(evidenceValue({ facts: { nested: { a: 1 } } }, "nested")).toBeNull();
  });

  it("reads a step's branch and commits from its delivery section only", async () => {
    const { branchOf, commitsOf } = await import("@/lib/neutral-names");
    expect(branchOf({ branch: "old", delivery: { branch: "new" } } as never)).toBe("new");
    expect(branchOf({ branch: "old" } as never)).toBeNull();
    expect(commitsOf({ delivery: { commits: ["b"] } })).toEqual(["b"]);
    expect(commitsOf({ commits: ["a"], delivery: { commits: [] } } as never)).toEqual([]);
    expect(commitsOf({ commits: ["a"] } as never)).toEqual([]);
    expect(commitsOf(null)).toEqual([]);
  });
});
