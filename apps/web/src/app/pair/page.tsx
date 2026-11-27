// SPDX-License-Identifier: Apache-2.0
"use client";

import { useEffect, useRef, useState } from "react";
import { ErrorNote, Heading, Lead } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { pairingCodeIn } from "@/lib/device-pairing";

/**
 * Where a phone lands when it scans the pairing code the computer shows (ADR 0228 §4).
 *
 * The owner, on the computer, opens My account › Devices and shows a QR code of
 * `https://<relay host>/pair#<code>`: the code is in the fragment, which the browser never sends to a server, so
 * neither the relay nor the edge receives or logs it. This page takes it out of the address bar at once (a reload
 * or the history does not hold it), posts it to `POST /auth/device/session` through the relay, and, once paired,
 * goes to the home page. A pairing lives five minutes and is exchanged once: a refused one is a stale or used
 * link, and the page says to show a new one on the computer.
 */
export default function PairPage() {
  const [state, setState] = useState<"pairing" | "missing" | "refused" | "elsewhere" | "failed">("pairing");
  const [failure, setFailure] = useState("");
  const started = useRef(false);

  useEffect(() => {
    // Once per page: React runs an effect twice in development, and a pairing is exchanged once.
    if (started.current) return;
    started.current = true;
    const { pathname, search, hash } = window.location;
    const code = pairingCodeIn(hash);
    window.history.replaceState(window.history.state, "", pathname + search);
    const exchanged = code ? api.exchangeDevicePairing(code) : Promise.reject(new Error("no code"));
    exchanged.then(
      () => window.location.replace(new URL("/", window.location.origin).toString()),
      (error: unknown) => {
        if (!code) setState("missing");
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
          Pairing this phone
        </Heading>
        <Lead>The code your computer showed signs this phone in to your installation. No password.</Lead>
      </div>
      {state === "pairing" && (
        <p role="status" className="text-sm text-ink-muted">
          Checking the pairing code…
        </p>
      )}
      {state === "missing" && (
        <ErrorNote>
          This page needs the link the computer&apos;s pairing code carries. On the computer, open My account ›
          Devices and scan the code it shows.
        </ErrorNote>
      )}
      {state === "refused" && (
        <ErrorNote>
          This pairing code has expired or was already used. On the computer, show a new one under My account ›
          Devices, and scan it within five minutes.
        </ErrorNote>
      )}
      {state === "elsewhere" && (
        <ErrorNote>A pairing code is opened on the phone, through the relay: scan it with the phone&apos;s camera.</ErrorNote>
      )}
      {state === "failed" && <ErrorNote>{failure}</ErrorNote>}
    </div>
  );
}
