// SPDX-License-Identifier: Apache-2.0
"use client";

import Link from "next/link";
import { Smartphone } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Button, Card, Code, ErrorNote } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { countdown, secondsLeft } from "@/lib/device-pairing";
import { encodeQr, qrPath, QUIET_ZONE } from "@/lib/qr";
import type { DevicePairing } from "@/lib/types";

type Refusal = "stale" | "elsewhere" | "no-relay" | "failed";

/** What a refused creation says, by the API's answer (ADR 0228 §4). */
function refusalOf(error: unknown): { refusal: Refusal; detail: string } {
  const detail = error instanceof Error ? error.message : String(error);
  if (error instanceof ApiError) {
    const first = (error.problem.errors ?? [])[0] as { error?: string } | undefined;
    if (error.status === 401 && first?.error === "step_up_required") return { refusal: "stale", detail };
    if (error.status === 404) return { refusal: "elsewhere", detail };
    if (error.status === 409) return { refusal: "no-relay", detail };
  }
  return { refusal: "failed", detail };
}

/**
 * My account › Devices, on the computer a personal installation runs on: pairing a phone (ADR 0228 §4).
 *
 * The owner, signed in on the loopback door less than ten minutes ago, asks for a pairing: a link to the relay
 * (`https://<relay host>/pair#<code>`) valid five minutes and exchanged once, shown here as a QR code for the
 * phone's camera. The phone's `/pair` page exchanges it and is signed in as the owner; it is then listed under
 * My account's sessions, where it is ended. The code is never shown as text and never kept: leaving the page,
 * or asking for a new one, forgets it.
 */
export default function DevicesPage() {
  const [pairing, setPairing] = useState<DevicePairing | null>(null);
  const [refused, setRefused] = useState<{ refusal: Refusal; detail: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!pairing) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [pairing]);

  const left = pairing ? secondsLeft(pairing.expires_at, now) : 0;

  async function showCode() {
    setBusy(true);
    setRefused(null);
    setPairing(null);
    try {
      const created = await api.createMyDevicePairing();
      setNow(Date.now());
      setPairing(created);
    } catch (error) {
      setRefused(refusalOf(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={Smartphone}
        title="Devices"
        description="Pair a phone with this installation: it reaches it through the relay, signed in as you."
      />
      <Card title="Pair a phone">
        <div className="space-y-4 text-sm">
          <p>
            Show a pairing code, then scan it with the phone&apos;s camera within five minutes. It opens this
            installation through its relay and signs the phone in as you, once. The phone is then listed under{" "}
            <Link href="/account#sessions">My account › Sessions</Link>, where you can sign it out.
          </p>
          {pairing && left > 0 && (
            <div className="space-y-2">
              <PairingCode url={pairing.url} />
              <p role="status" className="text-xs text-ink-muted">
                Valid for {countdown(left)}, for one phone. Anyone who scans it signs in as you: show it to your
                own phone only.
              </p>
            </div>
          )}
          {pairing && left === 0 && (
            <p role="status" className="text-xs text-ink-muted">
              This pairing code has expired. Show a new one.
            </p>
          )}
          {refused?.refusal === "stale" && (
            <ErrorNote>
              Pairing a phone needs a sign-in less than ten minutes old. Run <Code>choregos local open</Code> on this
              computer, then come back here.
            </ErrorNote>
          )}
          {refused?.refusal === "elsewhere" && (
            <ErrorNote>
              A phone is paired from the computer the installation runs on, at its local address: not through the
              relay, and only on a personal installation.
            </ErrorNote>
          )}
          {refused?.refusal === "no-relay" && (
            <ErrorNote>
              This installation is reachable through no relay yet, so a phone could not open the code. Run{" "}
              <Code>choregos local relay enable</Code> on this computer, then show a pairing code.
            </ErrorNote>
          )}
          {refused?.refusal === "failed" && <ErrorNote>{refused.detail}</ErrorNote>}
          <Button onClick={() => void showCode()} disabled={busy}>
            {pairing ? "Show a new pairing code" : "Show a pairing code"}
          </Button>
        </div>
      </Card>
    </div>
  );
}

/** The pairing link as a QR code, dark on light whatever the theme: a camera reads it, a person does not. */
function PairingCode({ url }: { url: string }) {
  const modules = useMemo(() => encodeQr(url), [url]);
  const side = modules.length + QUIET_ZONE * 2;
  return (
    <svg
      role="img"
      aria-label="Pairing code: scan it with your phone's camera"
      viewBox={`0 0 ${side} ${side}`}
      shapeRendering="crispEdges"
      className="size-64 rounded bg-white text-black"
      data-testid="pairing-code"
    >
      <path d={qrPath(modules)} fill="currentColor" />
    </svg>
  );
}
