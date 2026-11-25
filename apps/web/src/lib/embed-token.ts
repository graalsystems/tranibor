// SPDX-License-Identifier: Apache-2.0
/**
 * The frame token of the embedded component (S63-12, API `embed_frame`): the session of a frame that a host on
 * ANOTHER site holds, since the console's cookie is `SameSite=Lax` and is not sent there. It is minted for the
 * person by the sign-in popup (first party), handed to the frame by `postMessage` (same origin), kept in the
 * frame's memory (and its `sessionStorage`, to survive a reload of the frame) and sent as `X-Tranibor-Embed`
 * (the API still reads the former `X-Choregos-Embed`, ADR 0226).
 * The host application never sees it.
 */
import { storedName } from "./stored-names";

/** What the sign-in popup says to the frame that opened it. */
export const SIGNED_IN_MESSAGE = "tranibor.embed.signed-in";
/** What a popup of the previous release says (ADR 0226, R9c): accepted too until 0.32.0, during a rolling upgrade. */
export const SIGNED_IN_MESSAGES: readonly string[] = [SIGNED_IN_MESSAGE, "choregos.embed.signed-in"];
export const EMBED_HEADER = "X-Tranibor-Embed";
const KEY = storedName("embed.token");

type Held = { token: string; expiresAt: number };
let held: Held | null = null;
/** A token was held in this page: a 401 afterwards is its expiry, not a console session to sign in again. */
let everHeld = false;

function read(): Held | null {
  if (held) return held;
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (raw) {
      held = JSON.parse(raw) as Held;
      everHeld = true;
    }
  } catch {
    held = null;
  }
  return held;
}

/** Keeps a token for `expiresIn` seconds. */
export function setEmbedToken(token: string, expiresIn: number, now = Date.now()): void {
  held = { token, expiresAt: now + expiresIn * 1000 };
  everHeld = true;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(held));
  } catch {
    /* a private window: the token lives in memory only */
  }
}

export function clearEmbedToken(): void {
  held = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* nothing to remove */
  }
}

/** The header to add to a request, or nothing when there is no live token (the cookie then speaks, or nobody). */
export function embedHeaders(now = Date.now()): Record<string, string> {
  const current = read();
  if (!current) return {};
  if (current.expiresAt <= now) {
    clearEmbedToken();
    return {};
  }
  return { [EMBED_HEADER]: current.token };
}

/** Did this frame ever hold a token? Then its 401 is answered by asking the person, never by navigating away. */
export function isFrameSession(): boolean {
  read();
  return everHeld;
}
