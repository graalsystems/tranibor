// SPDX-License-Identifier: Apache-2.0
"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { OrgPageHeader } from "@/components/org-page-header";
import { Badge, Card, Code, Empty, ReadState } from "@/components/ui";
import { api } from "@/lib/api";
import { configureHref, instanceState } from "@/lib/plugins";
import { useSession } from "@/lib/session";

/**
 * Library → Plugins → one profile (S86-05, ADR 0224): the contract of tools a plugin declares, the servers
 * that say they implement it and the instances the organisation runs of each. Read only: instances are
 * added and deleted on the plugin's card, where the server is.
 */
export default function ProfilePage() {
  return (
    <Suspense>
      <Profile />
    </Suspense>
  );
}

function Profile() {
  const { org } = useSession();
  const name = useSearchParams().get("name") ?? "";
  const profiles = useQuery({ queryKey: ["plugin-profiles", org], queryFn: () => api.pluginProfiles(org) });
  const profile = profiles.data?.items.find((p) => p.profile === name);
  return (
    <div className="space-y-6">
      <OrgPageHeader title={name || "Profile"} />
      <p className="text-sm">
        <Link href="/library/plugins" className="underline">
          Back to the plugins
        </Link>
      </p>
      <ReadState lecture={profiles} quoi="the plugin profiles" />
      {profiles.data && !profile && (
        <Empty title="No such profile">
          No installed plugin declares <span data-verbatim>{name || "this profile"}</span>.
        </Empty>
      )}
      {profile && (
        <>
          <p className="max-w-3xl text-sm text-ink-muted" data-verbatim>
            {profile.description}
          </p>
          <p className="max-w-3xl text-sm text-ink-muted">
            Declared by <strong data-verbatim>{profile.plugin}</strong>. An agent that names this profile in its
            <Code>mcp_servers</Code> gets the tools below from whichever instance the organisation runs of a server that
            implements it.
          </p>
          <section className="space-y-2" aria-labelledby="profile-tools">
            <h2 id="profile-tools" className="text-lg font-semibold">
              Tools ({profile.tools.length})
            </h2>
            <ul className="space-y-1 text-sm" data-testid="profile-tools">
              {profile.tools.map((tool) => (
                <li key={tool.name} className="flex flex-wrap items-baseline gap-2">
                  <Code>{tool.name}</Code>
                  <Badge tone={tool.access === "read" ? "neutral" : "warn"}>{tool.access}</Badge>
                  {tool.description && <span className="text-xs text-ink-muted" data-verbatim>{tool.description}</span>}
                </li>
              ))}
            </ul>
          </section>
          <section className="space-y-3" aria-labelledby="profile-implementations">
            <h2 id="profile-implementations" className="text-lg font-semibold">
              Implementations ({profile.implementations.length})
            </h2>
            {profile.implementations.length === 0 ? (
              <Empty title="Nothing implements it">
                No installed server says it implements this profile; an agent that names it has no tool until one is
                installed.
              </Empty>
            ) : (
              profile.implementations.map((implementation) => (
                <Card
                  key={`${implementation.plugin}/${implementation.server}`}
                  title={
                    <h3 className="text-sm font-semibold" data-verbatim>
                      {implementation.plugin} / {implementation.server}
                    </h3>
                  }
                  data-testid={`implementation-${implementation.plugin}-${implementation.server}`}
                >
                  <ul className="space-y-1 text-sm" aria-label={`Instances of ${implementation.server}`}>
                    {implementation.instances.map((instance) => {
                      const state = instanceState(instance);
                      return (
                        <li key={instance.name} className="flex flex-wrap items-center gap-2" data-testid={`instance-${instance.name}`}>
                          <Code>{instance.name}</Code>
                          {instance.default && <Badge tone="accent">Default</Badge>}
                          <Badge tone={state.tone}>{state.label}</Badge>
                          <Link href={configureHref(instance.name)} className="text-xs underline">
                            Connector
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                </Card>
              ))
            )}
          </section>
        </>
      )}
    </div>
  );
}
