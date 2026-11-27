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

/**
 * Where to go once signed in: `next`, when it is a page of the console, else the home page.
 *
 * `next` is resolved as the browser resolves it, then judged by the origin it lands on. Its text alone says
 * too little: the URL parser strips a tab, a line feed and a carriage return, so `/%09/evil` passes a "one
 * leading slash" test and then resolves to `//evil`, another host. What is returned is the target's path,
 * query and fragment, kept only when they resolve to this origin again: `/.//evil` lands here on the path
 * `//evil`, which names a host once the page resolves it.
 */
export function nextPath(search: string, origin: string = window.location.origin): string {
  const next = new URLSearchParams(search).get("next");
  if (!next) return "/";
  let target: URL;
  try {
    target = new URL(next, origin);
  } catch {
    return "/";
  }
  if (target.origin !== origin) return "/";
  const path = target.pathname + target.search + target.hash;
  return new URL(path, origin).origin === origin ? path : "/";
}
