# 0226 — The former name leaves every layer, the wire included: each name gets its Tranibor twin, both are read for a window, the former one goes

- **Status**: accepted, 2026-11-25 (decided by the product owner on 2026-11-25: "the name is to go",
  every layer including the protocol)
- **Concerns**: everything [ADR 0176](0176-the-core-is-named-tranibor.md) left under the former name:
  the native CLI files and the install scripts, the console scripts of the images, the CLI's profile
  directory and the repository directory a workflow reads, the chart's directory, object names, values
  keys and default Secret names, and the wire and state names of ADR 0176 §6 (headers, manifest
  `apiVersion` and kinds, labels and annotations, the identity provider's group prefix, cookies, tracker
  markers, the MCP server's `name`, the A2A extension URI and problem types, the web fetch's tokens);
  amends ADR 0176 §6 ("deliberately not renamed") and ADR 0156 (the publishing identity names the
  former names only as aliases)

## Context

ADR 0176 renamed the visible name, the Python packages, the images and charts and the environment
variables, each behind a compatibility window, and kept the rest under the former name on purpose: a
party outside the repository holds it (a client that sends a header, a manifest a customer stored, a
cookie in a browser, a group in an identity provider), so a rename needs that party to move first.

On 2026-11-25 the product owner decided that the former name leaves **every** layer, the protocol
included. Measured on `main` the same day, it still appears about 25,000 times; in the public
documentation alone, 581 times in the 0.29.4 pages, and nearly every one is a real identifier a reader
must type: the command of the native CLI (the install script still puts `choregos` on the PATH), the
`choregos-admin` command, `.choregos/workflow.yaml`, `X-Choregos-Secret`, `apiVersion: choregos/v1`,
`choregos.dev/*` labels, `choregos:<org>:<group>` groups, the chart's object names (`choregos-api`) and
values keys (`choregos-api.runDoor.hostnames`). The documentation cannot drop the name before the
product does: a page that writes `tranibor login` to a reader whose binary is named `choregos` is wrong.

## Decision

1. **One rule for every remaining name: a twin, a window, a retirement.** Each name gets its Tranibor
   form. During one **window of two minor versions** from the release that ships the twin, the product
   **reads both** (accepts both on input, finds both in stored state) and **writes the new one** (emits,
   stores, renders the new name; emits both only where a reader of the former one exists outside the
   repository, and says where). The former name is removed in the minor that closes the window, in its
   own PR, never earlier. Nothing is renamed in place without its twin: a client, a manifest, a cookie or
   a group that worked the day before keeps working the day after.
2. **R5, commands and files.** The native CLI files become `tranibor-<platform>` (release assets, the
   API image's `/opt/<prefix>/cli/`, the manifest), the install scripts put `tranibor` on the PATH and,
   for the window, a `choregos` link to it; `self update` accepts both asset names. The images' console
   scripts gain `tranibor-admin`, `tranibor-runner`, `tranibor-outpost`, `tranibor-tools` beside the
   former ones. The CLI's profile is `~/.config/tranibor/config.json`, the former file read when the new
   one is absent and never written again. The repository directory a workflow reads is `.tranibor/`
   (`workflow.yaml`, `inputs/`, `facts.json`, `fields.json`), `.choregos/` read when `.tranibor/` is
   absent. The CLI reference is generated under `tranibor`.
3. **R6, the chart.** The chart's directory and name become `tranibor`, and its subcharts and the objects
   it renders `tranibor-*` (`tranibor-api`, `tranibor-web`…). The values keys `choregos-api`,
   `choregos-web`, `choregos-orchestrator`… are read as aliases of the new keys for the window (a helper
   merges them; a key set under both names refuses to render). Default Secret names are stored state
   (point 6) and keep their former names (amended 2026-11-25: a renderer that cannot read the cluster
   would not find a renamed Secret, and would draw new keys); a deployment renames them by migration,
   copying each Secret and then setting the new name in its values. **An object that holds data is not
   renamed by a rename**: PersistentVolumeClaims and the StatefulSets that own them keep their names
   until the operator runs the migration the chart documents; the upgrade notes give each step.
4. **R7, the wire (a `contract-change` PR, ADR 0001).**
   - Headers `X-Tranibor-*` beside `X-Choregos-*`: every receiver accepts both, every sender sends the
     new one; webhook signatures are computed the same way under either name.
   - Manifests: `apiVersion: tranibor/v1` and `tranibor.dev/v1alpha1` accepted beside the former ones;
     the platform and the CLI write the new ones; a stored manifest is read as it was written.
   - Labels and annotations `tranibor.dev/*` written beside `choregos.dev/*` for the window (selectors and
     policies match either), then alone.
   - Identity provider groups `tranibor:<org>:<group>` accepted beside `choregos:<org>:<group>`.
   - Cookies set under `tranibor_*`, the former ones read and then cleared.
   - Tracker markers written as `tranibor:*`, both found.
   - The MCP server's `name`, the A2A extension URI, the problem type URIs and the web fetch's
     `User-Agent` and robots tokens gain the new form; the former stays accepted (or, for a token a
     third party keys on, sent beside) for the window.
   - **One value stays, because it is not a name but an input to a hash**: the UUID namespace that
     derives A2A task ids. Changing it changes every id. It is renamed in the code (a constant named for
     what it is), its value never changes, and no document shows it.
5. **R8, the documentation.** As each round ships, `docs/` and `docs/site/` write the new names only and
   say once, in the upgrade notes, which former name is still read until which version.
6. **Stored state is renamed by migration, not by a rename.** The database name and role, Temporal task
   queues and workflow types (ADR 0008: a renamed workflow type breaks replay) and the sealed-secret
   paths are deployment values or history: a deployment that wants new names gets a documented
   migration; a new deployment gets the new names by default.

## Consequences

- No running deployment, client or stored manifest breaks on upgrade; each loses the former name at the
  end of its window, announced in `docs/deployment.md`.
- The enterprise edition (its imports, its hosts' TXT record `_choregos-challenge`) moves inside the same
  windows; its own records follow.
- The public documentation can drop the former name page by page as each round ships, without writing a
  command or a key the reader's version does not have.
- The count of the former name in the repository is a measured target: a test lists where it may still
  appear (the compatibility code of each open window and the history: ADRs, changelogs, migrations) and
  fails on any new occurrence elsewhere.

## Rejected alternatives

- **Rename everything in one release.** Every client that sends `X-Choregos-Secret`, every stored
  manifest and every signed-in person would break at once.
- **Keep the wire names forever (ADR 0176 §6 as it was).** Rejected by the product owner: the former
  name stays visible to every integrator, in every document.
- **Rename only in the documentation.** The documentation would describe commands and keys the product
  does not have.
