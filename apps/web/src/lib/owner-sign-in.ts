// SPDX-License-Identifier: Apache-2.0
/**
 * What the owner's sign-in page reads from its address (ADR 0228 §4): the ticket in the fragment, and where to
 * go once signed in.
 */

/** A compact JWS: three base64url parts. */
const COMPACT_JWS = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;

/** The ticket the fragment carries (`#<ticket>`, as `choregos local open` writes it), or `""`. */
export function ticketIn(hash: string): string {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  return COMPACT_JWS.test(value) ? value : "";
}

/** Where to go once signed in: `next`, when it is a path of the console, else the home page. */
export function nextPath(search: string): string {
  const next = new URLSearchParams(search).get("next") ?? "";
  return next.startsWith("/") && !next.startsWith("//") && !next.includes("\\") ? next : "/";
}
