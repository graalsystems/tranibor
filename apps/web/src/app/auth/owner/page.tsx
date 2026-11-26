// SPDX-License-Identifier: Apache-2.0
"use client";

import { useEffect, useRef, useState } from "react";
import { Code, ErrorNote, Heading, Lead } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { nextPath, ticketIn } from "@/lib/owner-sign-in";

/**
 * Where the owner of a personal installation signs in, on the computer it runs on (ADR 0228 §4).
 *
 * `choregos local open` (or the desktop app) signs a ticket with the owner key and opens
 * `http://127.0.0.1:<port>/auth/owner#<ticket>`: the ticket is in the fragment, which the browser never
 * sends to a server, so no server receives or logs it. This page takes it out of the address bar at once
 * (a reload or the history does not hold it), posts it to `POST /auth/owner` and, once signed in, goes on
 * to `next` (a path of the console) or the home page. A ticket lives 60 seconds and is taken once: a
 * refused one is a stale or used link, and the page says to open a new one.
 */
export default function OwnerSignInPage() {
  const [state, setState] = useState<"signing-in" | "missing" | "refused" | "elsewhere" | "failed">("signing-in");
  const [failure, setFailure] = useState("");
  const started = useRef(false);

  useEffect(() => {
    // Once per page: React runs an effect twice in development, and a ticket is taken once.
    if (started.current) return;
    started.current = true;
    const { pathname, search, hash } = window.location;
    const ticket = ticketIn(hash);
    window.history.replaceState(window.history.state, "", pathname + search);
    const exchanged = ticket ? api.signInAsOwner(ticket) : Promise.reject(new Error("no ticket"));
    exchanged.then(
      () => window.location.replace(new URL(nextPath(search), window.location.origin).toString()),
      (error: unknown) => {
        if (!ticket) setState("missing");
        else if (error instanceof ApiError && error.status === 401) setState("refused");
        else if (error instanceof ApiError && error.status === 404) setState("elsewhere");
        else {
          setFailure(error instanceof Error ? error.message : String(error));
          setState("failed");
        }
      },
    );
  }, []);

  return (
    <div className="mx-auto max-w-md space-y-6 py-12">
      <div className="space-y-3">
        <Heading as="h1" size="xl">
          Signing you in
        </Heading>
        <Lead>The owner key of this installation signs you in on this computer. No password.</Lead>
      </div>
      {state === "signing-in" && (
        <p role="status" className="text-sm text-ink-muted">
          Checking the sign-in link…
        </p>
      )}
      {state === "missing" && (
        <ErrorNote>
          This page needs the link <Code>choregos local open</Code> opens. Run it on this computer.
        </ErrorNote>
      )}
      {state === "refused" && (
        <ErrorNote>
          This sign-in link has expired or was already used. Run <Code>choregos local open</Code> again.
        </ErrorNote>
      )}
      {state === "elsewhere" && (
        <ErrorNote>
          The owner key signs in only on the computer the installation runs on, at its local address.
        </ErrorNote>
      )}
      {state === "failed" && <ErrorNote>{failure}</ErrorNote>}
    </div>
  );
}

