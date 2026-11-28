# 0228 — ServiceNow is a built-in tracker of the core, beside the plugin

- **Status**: accepted, 2026-11-28 (asked by the product owner on 2026-11-28: "ServiceNow must be
  manageable in the app like Jira")
- **Concerns**: [ADR 0093](0093-an-agent-ships-as-a-plugin.md) (ServiceNow shipped as a plugin),
  [ADR 0096](0096-a-plugin-s-code-runs-as-an-mcp-server-the-platform-runs.md) (a plugin's server runs on
  Kubernetes), [ADR 0209](0209-the-azure-devops-family-boards-first-then-repos-and-pipelines.md) (the model
  followed), `packages/adapters` (`tracker/servicenow*.py`, `fakes/servicenow.py`, `specs.py`,
  `builtins.py`), `apps/api/src/tranibor_api/routers/webhooks.py` (`POST /webhooks/servicenow`),
  `docs/procedures` (`connector.tracker.servicenow`)

## Context

ADR 0093 made ServiceNow a plugin: an agent, its skills, its templates, its procedures and a server that
serves both the governed ITSM tools and the tracker profile (`tracker/plugin`). That server is a plugin's
code, and ADR 0096 runs a plugin's code only as an MCP server the platform deploys on Kubernetes. On Docker
Compose, on a laptop, or on any install without the plugin servers' namespace, a project therefore cannot use
ServiceNow as its tracker at all, while Jira works anywhere with a URL and a token, and Azure Boards since
ADR 0209 the same way.

On 2026-11-28 the product owner asked for parity: a ServiceNow instance chosen and configured in a project
exactly like Jira, from the tracker connector's form.

## Decision

1. **A core adapter `tracker/servicenow`.** The core ships a `TrackerAdapter` for ServiceNow, registered as
   connector kind `tracker`, type `servicenow`, like `jira` and `azure-boards`: a spec (`instance_url`,
   `auth: oauth | basic`, `client_id` or `username`, `assignment_groups`, optional `tables`), three sealed
   secret fields (`password`, `client_secret`, `webhook_secret`, ADR 0058), a configuration check, an egress
   (the instance's host only), a setup procedure, and the scriptable `FakeServiceNowInstance` (ADR 0005;
   under `TRANIBOR_FAKES=1` the registry builds the generic fake tracker, as for every external tracker).
2. **Ported, not reinvented.** The adapter is the plugin server's tracker (`tranibor-agents/plugins/
   servicenow/server`, Apache-2.0), moved into the core with its rules: the key is the record's number; every
   text is a **work note**, never a customer-visible comment; `priority` is never written; a creation files an
   incident, idempotent by `correlation_id = tranibor:<key>`; a state is a label matched case-blind against
   `sys_choice`, a closing state is refused there and closing is `close_item`'s, with its close code; a
   change request moves only through the Change Management API, to a state its model reaches; the poll's
   candidates are the open items of the assignment groups, newest first, bounded; every read names its
   fields and every write is checked against the answer (ServiceNow answers 200 to a write it did not apply);
   a value can never change an encoded query. The core's conventions replace the plugin's names: the status
   marker is `[tranibor:status]` (`wire.marker`), the webhook header `X-Tranibor-Secret`, the correlation
   prefix `tranibor:`.
3. **One webhook route, `POST /webhooks/servicenow`.** The Business Rule of the procedure posts
   `{table, sys_id, number, sys_mod_count, operation, state, instance}`. ServiceNow signs nothing: the secret
   is compared in constant time with each ServiceNow connector's `webhook_secret` (a match routes only to
   that connector's projects), or with the platform's shared secret, as for Jira and Azure Boards (then the
   `instance` the delivery names, then the project that already holds the record, tell projects apart; an
   ambiguity routes nothing). Each record is de-duplicated on `sys_id:sys_mod_count` and re-read through the
   connector before anything acts on it.
4. **The plugin stays.** It keeps its agent, skills, templates and procedures, and may keep its server for
   the governed ITSM tools. A project picks either the built-in tracker (`tracker/servicenow`) or the
   plugin's (`tracker/plugin`), never both; nothing migrates by itself.

## What is ported, and what is not

- **Ported**: the Table API client (basic, OAuth client credentials with a cached token renewed once on a
  401), the closed table and field lists, the typed encoded query, the `sys_choice` states (and `task`'s for a
  request item, and the out-of-box list when `sys_choice` is refused), the journal read from
  `sys_journal_field` and, when an ACL hides it, from the record's rendered journal fields, the change
  model's next states, the Business Rule's payload and its verification.
- **Mapped to the core's protocol**: `set_fields` writes ServiceNow's updatable fields (impact, urgency,
  category, subcategory, assignment group, assigned to, configuration item) and refuses `priority` and any
  other; the bookkeeping fields every board receives (cost, size, risk, run) are skipped, the status note
  carries them. `link` is a work note naming the other item: ServiceNow has no generic link between an
  incident, a request item and a change. Labels do not exist: a project starts items by a start rule.
- **Not ported**: knowledge articles, the CMDB and its relations, similar incidents, a customer-visible
  comment, a resolution or a change request filed by an agent. They are governed tools an agent calls under
  approval, and stay the plugin's.

**In the console.** ServiceNow is a tracker of the new-project page and of the project's settings, like Jira. The
new-project page asks the chosen external tracker's settings and secrets (the settings' own connector form, for
that type) and writes them with the project; it saved an empty configuration before, which every external
tracker's check refuses.

## Consequences

- ServiceNow works wherever the core works; the tracker's writes are platform calls through the connector,
  audited like Jira's, without a plugin server in between.
- Two implementations of one tracker exist for a while: the plugin's server and the core adapter. A fix to
  one is checked against the other until the plugin drops its tracker profile, which is the plugin's
  decision.
- The adapter is proven against a scripted instance and the plugin server's records of a live one; not
  against a live instance from this repository. Its procedure says so.
- A route was added to the API contract (`servicenowWebhook`), and the procedures' webhook sources gained
  `servicenow`.

## Rejected alternatives

- **Run the plugin's server outside Kubernetes.** It would bring third-party code into the API's process or
  a container the platform does not isolate, against ADR 0096; and it still needs a plugin installed for a
  tracker the product owner wants in the connector form.
- **Make the core call the plugin server's code as a library.** The core never depends on a plugin; a copy
  under the core's conventions, tested in the core, is what Jira and Azure Boards are.
- **Route by the platform's shared secret only.** The payload carries no project; with several instances on
  one platform, the per-connector `webhook_secret` is what tells them apart without guessing.
