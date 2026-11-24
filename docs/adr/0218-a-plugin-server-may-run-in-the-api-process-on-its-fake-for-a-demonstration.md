# 0218 — A plugin server may run in the API's process, on its fake, for a demonstration

- **Status**: accepted, 2026-11-24
- **Concerns**: `packages/core` (`production.refuse_demo_plugin_servers`), `packages/adapters`
  (`fakes/in_process_plugin_servers.py`), `apps/api` (`config`, `services/plugin_servers.py`, the lifespan);
  builds on [ADR 0005](0005-adapters-and-fakes.md), [ADR 0096](0096-a-plugin-s-code-runs-as-an-mcp-server-the-platform-runs.md),
  [ADR 0212](0212-the-api-can-run-the-workers-inline-for-a-demonstration.md) and
  [ADR 0213](0213-the-demonstration-mcp-server-is-the-fakes-with-tools-declared-in-a-file.md)

## Context

A plugin's code runs only as an MCP server the platform deploys on Kubernetes (`plugin_servers: kubernetes`,
ADR 0096). On a laptop (the API alone under `TRANIBOR_FAKES=1`, or `docker compose`) a plugin that brings a
server cannot be installed usefully: the runtime is an in-memory `FakeKubernetesApi` that runs nothing, and
the broker's calls go to `FakePluginTrackerServer`, which serves the eight `tracker_*` tools only. The
ServiceNow plugin, whose server has a seeded fake instance, therefore could not be shown without a cluster
(S80-20 says so). The eval matrix already serves a plugin's fake in process (`use_fake_plugin_server`), but
from a recording or a transport a test registers, never from the plugin's own code.

## Analysis

Possible without a large change: the whole governed path (connector, discovery, operation policies, approvals,
the broker's client) goes through one `PluginServerClient` with an `httpx` transport that `plugin_servers.transport()`
already chooses. An `httpx.ASGITransport` over the plugin's own ASGI application is such a transport. What is
missing is only (1) a runtime that starts the application instead of a Deployment, (2) the code to import, and
(3) the install's image checks, which are meaningless when no image is pulled.

Rejected: a Docker socket (a root-equivalent handle the API must never hold), a localhost listener (a network
port that answers anyone), vendoring a plugin into the core (the core ships no plugin's code, ADR 0093).

## Decision

1. **`TRANIBOR_DEMO_PLUGIN_SERVERS=<file>` runs plugin servers in the API's process.** In that mode every server
   of an installed plugin is run from a Python package, behind an `httpx.ASGITransport`, on the host
   `<object>.in-process.invalid` (no resolver serves it). No Deployment, Service, Secret or NetworkPolicy is
   written. The broker, discovery, the operation policies and the approvals are unchanged.
2. **Where the code is is the operator's to say.** The file lists, per `plugin`/`server`, `app`
   (`module:callable` building the ASGI application), optionally `settings` (`module:callable(env)`), `path`
   (a directory put on `sys.path`; or an installed wheel) and `fake_env` (extra variables, such as
   `SERVICENOW_FAKE_SEED`). The core imports nothing at load time and ships nothing. The environment given to the
   application is the declaration's `fake.env`, the file's `fake_env` and the platform's token
   (`TRANIBOR_SERVER_TOKEN`), which the application checks as it does behind a Service. No contract changes:
   the plugin's `fake:` block is read as it is.
3. **Only a server that declares its fake, and that the file names, runs.** Anything else is a 422 at the install.
   The image is neither checked nor pulled in this mode (and only in it): the install from a local directory
   without `plugins.lock` is the existing offline source.
4. **Refused elsewhere.** `refuse_demo_plugin_servers` (like ADR 0212 and 0213) refuses unless `TRANIBOR_FAKES`
   is on and `TRANIBOR_ENV` is `dev` or `test`: at the API's start, in the install's check, and in the adapter
   that reads the file, whoever calls it.
5. **Lifecycle.** The application starts at install (an import failure fails the install) and again, when the
   process forgot it, at the first call (`server_endpoint`); a replay with the same environment is a no-op. Its
   ASGI lifespan runs in a task of its own; an uninstall and the API's shutdown stop it.

## Limits

In memory: a restart starts the fake again from its seed. The API and the workers that play an approved action
must be one process (ADR 0212). A person's exchanged token (ADR 0135) is not accepted by the fake. The
connector's configuration is not read. No eval instance. The package's dependencies must be importable by the
API's environment, which is the operator's to install.

## Consequences

`docs/plugins.md` ("On a laptop: a server in the API's process") and `docs/development.md` say how to run it;
`examples/demo-plugin-servers/servicenow.yaml` is a sample. `apps/api/tests/test_in_process_plugin_servers.py`
proves the chain with a small package of the tests' own (`tests/fixtures/plugin_servers/demo_itsm_server`) and
the refusals.
