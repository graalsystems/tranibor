// SPDX-License-Identifier: Apache-2.0
/**
 * The organisation the person last chose, remembered in the browser (S42-13).
 *
 * `localStorage` keeps the choice for the client; the non-HttpOnly cookie `tranibor_org` carries
 * the same slug to the server, so the root layout can draw that organisation's theme (name, logo,
 * colours) on the first paint, sign-in page included. The cookie only SELECTS a public look: it
 * grants nothing, every API route still checks the session, and an unknown or malformed value
 * means the instance theme.
 *
 * Before ADR 0226 the cookie was `choregos_org`: the server still reads it when the new one is absent, and
 * the next choice writes the new one and clears it.
 */
import { storedName } from "./stored-names";

export const ORG_COOKIE = "tranibor_org";
export const FORMER_ORG_COOKIE = "choregos_org";
export const ORG_STORAGE_KEY = storedName("org");

/** The same shape the API accepts for an organisation slug (`theme.ORG_SLUG`). */
const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** The slug, or null: anything else (a path, a query, an overlong string) is never trusted. */
export function validOrgSlug(value: unknown): string | null {
  return typeof value === "string" && SLUG.test(value) ? value : null;
}

/** Remembers `slug` for the server (cookie, one year, this site only). */
export function rememberOrgCookie(slug: string): void {
  const safe = validOrgSlug(slug);
  if (!safe || typeof document === "undefined") return;
  const secure = typeof window !== "undefined" && window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${ORG_COOKIE}=${safe}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
  document.cookie = `${FORMER_ORG_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax${secure}`;
}

/** The slug remembered by `localStorage`, validated; null without one or without storage. */
export function rememberedOrg(): string | null {
  try {
    return validOrgSlug(window.localStorage.getItem(ORG_STORAGE_KEY));
  } catch {
    return null;
  }
}

/**
 * The organisation to show: the remembered one, followed through a rename (ADR 0090). `wanted` may be
 * a slug the organisation had when it was remembered; `Org.aliases` lists those, so the choice is
 * kept instead of silently falling back to the first organisation. Null when `orgs` is empty.
 */
export function pickOrg(orgs: { slug: string; aliases?: string[] }[], wanted: string | null): string | null {
  const found = orgs.find((o) => o.slug === wanted) ?? orgs.find((o) => wanted !== null && o.aliases?.includes(wanted));
  return found?.slug ?? orgs[0]?.slug ?? null;
}
