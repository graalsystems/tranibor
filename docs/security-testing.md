# Security testing: fuzzing the API against its contract

This page says what was run against the API before a third-party penetration test, what it found, and
what it did not look at. **It is not a penetration test.** A fuzzer sends inputs the contract allows and
inputs it forbids, and compares the answers with the contract. It does not chain steps, abuse business
logic, test the infrastructure, or think like an attacker. It finds the cheap things first, so that the
testers' time goes to the others. The [security page](security.md) says what the platform protects and
what is not a wall; this page is the evidence for one slice of it.

## What was run

| | |
|---|---|
| Tool | Schemathesis 4.34.2, run from a separate tool environment (`uvx --from schemathesis schemathesis run ...`), never added to the project's lock |
| Target | the API at `0.29.4` (commit `32ceb2b8f`), started alone: fakes on (`TRANIBOR_FAKES=1`), `TRANIBOR_ENV=dev`, SQLite, the development sign-in, two organisations (`acme`, `globex`) with an `org_admin` each, one project each |
| Contract | `packages/contracts/openapi.yaml`: 483 operations (397 paths) |
| Credentials | an API token of an `org_admin` (never recorded here), for the authenticated pass; none for the anonymous pass |

Three passes:

1. **Authenticated, as an organisation administrator.** `--checks all --max-examples 8 --phases fuzzing
   --workers 2`. 481 of 483 operations tested (the two that take a zip, `POST /orgs/{org}/hosted-sources`
   and `POST /orgs/{org}/skills/import`, need a serializer the tool does not have). 15,216 test cases in
   699 seconds. Checks: server errors, response schema conformance, undocumented status codes and content
   types, schema-valid requests rejected, schema-violating requests accepted, and the two authentication checks.
2. **Anonymous.** No credentials, `--checks not_a_server_error,status_code_conformance,ignored_auth
   --max-examples 4`. 481 operations, 4,824 cases, 101 seconds. A second, hand-written sweep sent one
   minimal request to every operation without credentials and listed every answer that was not a 401 or a 403.
3. **Two organisations.** The tool cannot do this, so a script did: for every operation (the event streams
   left out) it called the organisation's and the project's identifiers of one organisation with the
   administrator token of the other, in both directions, 950 requests. A 2xx that carried the other
   organisation's data, or a 5xx, was a finding.

The commands, abridged (the token is read from a file):

```bash
TRANIBOR_FAKES=1 TRANIBOR_ENV=dev TRANIBOR_DEV_LOGIN_ENABLED=true \
  TRANIBOR_DEV_ADMIN_EMAILS=admin-a@a.dev TRANIBOR_OIDC_DEFAULT_ORG=acme \
  TRANIBOR_DATABASE_URL=sqlite+aiosqlite:///fuzz.db uv run uvicorn tranibor_api.main:app --port 8010
# sign in with /auth/login?as=<email>, once per organisation (TRANIBOR_OIDC_DEFAULT_ORG says which),
# then POST /me/tokens with the console's Origin header to get a token

uvx --from schemathesis schemathesis run packages/contracts/openapi.yaml --url http://localhost:8010/api/v1 \
  -H "Authorization: Bearer $(cat token-a)" --checks all --max-examples 8 --workers 2 --phases fuzzing
uvx --from schemathesis schemathesis run packages/contracts/openapi.yaml --url http://localhost:8010/api/v1 \
  --checks not_a_server_error,status_code_conformance,ignored_auth --max-examples 4 --workers 2 --phases fuzzing
```

## What it found

Counts are the tool's, by check (one failure can carry several checks): 679 unique failures in the
authenticated pass, of which 276 undocumented status codes, 150 + 7 authentication, 138 schema-valid
requests refused, 93 responses off the contract, 9 server errors and 5 schema-violating requests accepted.
Triage put each in one of four boxes.

### Real bugs, fixed

| Route | Input | Observed | Fix |
|---|---|---|---|
| `POST /webhooks/github`, `/jenkins`, `/tekton` (and by reading the code `/gitlab`, `/alertmanager`, `/argocd`) | a body of `\x00`, not JSON, or JSON that is not an object | 500 (`JSONDecodeError` in the handler) | 400 (#2243) |
| `PATCH /orgs/{org}/knowledge/{slug}` | `{"scope": null, "status": "active"}` | 500 (`NOT NULL constraint failed: knowledge_bases.scope`) | a null scope means unchanged (#2243) |
| `PUT /projects/{id}/models` | `max_turns_factor: 0` | 500 (the configuration model refuses it after the request schema accepted it) | 422 (#2243) |
| `POST /orgs/{org}/agents/discover` | `{"card_url": "https://"}` | 500 (`ValueError: an empty host` in the egress guard) | 422 (#2243) |
| `GET /connectors/types`, `/platform/models`, `/templates`, `/templates/{name}`, `/workflows/templates`; `POST /workflows/validate`, `/workflows/edit` | no credentials | 200 (the contract's default is authenticated). `/platform/models` lists the gateway's upstream models and providers | 401 (#2254) |
| `GET /projects/{id}` and the routes under it | the id of a project of another organisation, signed in with no role there | 403 whose detail names `org/slug` of the project (on SQLite, which has no row-level security) | 404 (#2254) |

Each has a test that fails without the fix. The 500s were on authenticated senders or administrators and
exposed nothing (the body is a generic problem with a correlation id), but a retried 500 is a loop for a
webhook sender and a stack trace in the logs for the operator.

### Contract gaps, fixed in the contract

The API sent responses its own schemas rejected: `WorkflowValidation.graph` and `.document` are null for
a document that does not parse; a validation error's `loc` holds indexes; a policy issue's `column` may
be null; `project.schema.json` lacked `dod` and `policy.schema.json` lacked
`express_lane.approval.timeout_hours`. #2246 (`contract(openapi)`), with a test per change.

### Contract gaps, not fixed

- **Undocumented statuses, 276 cases**: 400 (72), 401 (5), 403 (26), 404 (61), 409 (1), 422 (104). Every
  one is an answer the API gives on purpose and the operation does not list. A `default` problem response
  on every operation would close them; it is a contract-wide edit, left for its own PR.
- **A body valid under the schema, refused, 138 cases**: mostly 422 for rules the document does not say
  (an invalid cursor, a field's semantics). The Pydantic model is stricter than `openapi.yaml`.
- `POST /orgs/{org}/agent-drafts` returns `runtime: null` where the schema has a `oneOf` of six runtimes.
- `GET /procedures/{id}`, on a procedure that does not exist, answers `application/json` and
  `{"detail": ...}`; the contract says `application/problem+json`. It is a small code fix.
- `POST /auth/logout` answers 204 without a session. That is intended (it clears a cookie); the
  operation does not say it needs none.
- 502 and 503 from `GET /orgs/{org}/skills/{slug}/update`, `POST /orgs/{org}/skill-catalogue/{id}/install`
  and `GET /auth/login` are documented upstream failures (a fake with no recording, no identity provider
  on the bench), not defects.

### Tool noise, not a finding

- **"API accepts invalid authentication", 150 cases.** The document declares a cookie scheme next to the
  bearer one; the tool replaced the cookie with garbage and kept the valid bearer token, which the API
  rightly honours. The anonymous pass (below) is the real check.
- **Date-times without a zone, about 30 operations.** SQLite returns naive datetimes. PostgreSQL columns
  carry the zone, so the response validates there. Not re-checked on PostgreSQL in this run.
- **429, 404 on made-up identifiers**, and the stream endpoints the tool cannot read to the end.

### What held

- **Anonymous.** 364 of 481 operations answered only 401 or 403. The rest are the sign-in routes, the SAML
  and GitHub-App callbacks, the webhooks (authenticated by HMAC or shared secret, and refused in `staging`
  and `prod` when none is configured), the MCP gateway's public key, `/edition` and `/theme`, and the seven
  routes fixed above. No operation answered a 5xx without credentials except the webhook bodies above.
- **Two organisations.** In 950 cross-organisation requests, none returned the other organisation's data.
  The organisation-scoped routes answered 403, the list routes answered the caller's own (empty) set, and the
  one disclosure was the project-by-id message fixed above.

## What was not covered

- **WebSockets and server-sent events.** The tool reads a response to the end; the streams (`/orgs/{org}/activity/stream`
  and the like) were left out of the sweeps.
- **The console** (`apps/web`): no browser-side test, no XSS or CSP review, no session-handling test beyond the API.
- **The runner and the sandbox**: confinement, egress from a run, the agent's own tool calls.
- **Plugin servers and outposts**: the tracker-plugin servers, the outposts' door and the connector at an outpost.
- **The MCP gateway and the A2A door** as protocols: only their documented HTTP operations were fuzzed.
- **Authorization in depth.** A 422 on a `POST` with an empty body does not prove the authorization check ran
  first. The two-organisation script proves reads and the identifiers it could build; it does not prove every
  write is refused, nor that a role *inside* an organisation (viewer, contributor, project owner) is
  enforced on each route.
- **Business logic**, race conditions, the approval flow, resource exhaustion, rate limits, the deployment
  (Helm chart, network policies, Kyverno policies) and PostgreSQL row-level security (the run used SQLite).
- **Depth.** Eight examples per operation is a smoke run. A longer run, the stateful phase (a created
  resource fed into the next call), and a run on PostgreSQL with the real identity provider are the next steps.

## Running it again

```bash
uvx --from schemathesis schemathesis run packages/contracts/openapi.yaml \
  --url http://localhost:8000/api/v1 -H "Authorization: Bearer $TOKEN" \
  --checks all --max-examples 8 --workers 2 --phases fuzzing
```

Use a throwaway database: the run creates and deletes organisation data. Never run it against an
instance that holds real data, and never put the token in a file in the repository.
