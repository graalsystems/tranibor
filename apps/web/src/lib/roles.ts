// SPDX-License-Identifier: Apache-2.0

/** The roles, from least to most (`rbac.py`), under their neutral names (ADR 0181 C1). */
export const ROLES = ["viewer", "contributor", "approver", "project_owner", "org_admin"] as const;
export type Role = (typeof ROLES)[number];

/** A role as the API answered it. */
export function neutralRole(role: string): Role {
  return role as Role;
}
