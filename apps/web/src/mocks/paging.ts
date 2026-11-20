// SPDX-License-Identifier: Apache-2.0
import type { PageMeta } from "@choregos/contracts/api";

/**
 * A page of `items` the way the server pages (ADR 0206): `limit` (default 50) rows after the opaque
 * `cursor`, `meta.has_more` and a `meta.next_cursor` to pass back. `items` is already in the route's order.
 * The cursor of the mock is the offset, encoded: only the mock reads it.
 */
export function pageOf<T>(items: readonly T[], query: string | URLSearchParams): { items: T[]; meta: PageMeta } {
  const search = typeof query === "string" ? new URLSearchParams(query) : query;
  const limit = Math.max(1, Math.min(200, Number(search.get("limit") ?? 50) || 50));
  const cursor = search.get("cursor");
  const start = cursor ? Number(atob(cursor)) || 0 : 0;
  const slice = items.slice(start, start + limit);
  const more = start + limit < items.length;
  return {
    items: structuredClone(slice),
    meta: { has_more: more, next_cursor: more ? btoa(String(start + limit)) : null },
  };
}
