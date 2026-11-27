# 0228 — A personal installation is one person's production: its own posture, one member, no password

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends
  [ADR 0184](0184-a-supported-single-node-installation.md) §1,
  [ADR 0029](0029-a-platform-for-governed-agents.md) §3,
  [ADR 0196](0196-an-actions-approvers-come-from-its-work-item.md) §1 and §2, and plan decisions D9 and D11
  ([00-index.md](../plan/00-index.md), their rows amended in the same pull request), each for the personal
  posture only and quoted under "Amended, for this posture only")
- **Concerns**: `tranibor_core.production` (`PERSONAL_ENVIRONMENT`, `is_production`, `is_personal`) and every
  module that spelled `{"staging", "prod"}`; a new `apps/api/src/tranibor_api/personal_installation.py` (not
  `services/installation.py`, which installs a template's agents: the posture recorded, `sole_member`, the
  start-up check), the `installation` table and its SQLite triggers (`db/models/installation.py`), the trigger
  `choregos_sole_member()` (migration `20261127_1000_s87_10_sole_member.py`) and `bootstrap.py`; the decision
  paths `services/actions.py` (`_one_of_its_approvers`, `decide`), `services/agent_releases.py`,
  `services/human_assignee.py` (`decider_verdict`) and `freshness.py`; new `owner_sign_in.py` and `passkeys.py`,
  `routers/auth.py` (`redirection_sure`), `routers/device_sign_in.py`, `origin_guard.py`, `browser_sessions.py`;
  a new `deploy/personal/` (`compose.yaml`, `Caddyfile`); `charts/choregos/templates/guard.yaml`;
  `choregos local` (a new `packages/cli/src/tranibor_cli/local/`, ADR 0229); a new page `docs/personal.md`.
  Builds on [ADR 0010](0010-gates-are-mechanisms.md),
  [ADR 0101](0101-governance-the-platform-does-not-enforce-is-refused.md),
  [ADR 0130](0130-a-browser-session-is-recorded-and-its-owner-ends-it.md),
  [ADR 0144](0144-the-run-s-api-answers-only-where-runs-are.md) and
  [ADR 0170](0170-sign-in-security-moves-into-the-core.md). Siblings of the same day: the personal runner
  ([ADR 0226](0226-a-person-s-own-machine-runs-that-person-s-runs.md)), the subscription
  ([ADR 0227](0227-a-person-s-own-subscription-is-used-only-by-the-vendor-cli-on-that-person-s-runner.md)), the
  surfaces that install it
  ([ADR 0229](0229-surfaces-may-install-and-supervise-a-personal-installation-and-its-runner.md)), the relay
  ([ADR 0230](0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md)), Web Push
  ([ADR 0194](0194-a-person-s-devices-are-told-by-web-push.md))

## Context

The product owner wants one person to run Choregos for themself, on their own computer, and to use it from the
console there and from their phone (decisions of 2026-11-26). Three installations exist; none fits.

- `docker compose up` (`compose.yaml`) is a development install: `TRANIBOR_ENV: dev`, the development sign-in
  (`TRANIBOR_DEV_LOGIN_ENABLED: "true"`), PostgreSQL started with `fsync=off`, and a worker that mounts
  `/var/run/docker.sock` for its `local_docker` executor. Once a phone reaches it through a relay (ADR 0230),
  the development sign-in opens it to anyone; and whoever holds the Docker socket can `docker inspect` the
  container that carries the person's subscription token (ADR 0227 §6).
- The single-node profile (ADR 0184) is the chart on one k3s machine of 8 vCPU and 32 GiB, with an OIDC provider
  the site brings. A laptop under Docker Desktop or Podman meets neither condition.
- The chart on a cluster (plan decision D9) is an organisation's.

Production was `staging` or `prod`, spelled as a literal set in each module that refused something there
(`config.py`, `main.py`, `routers/connectors.py`, `routers/org_connectors.py`, `routers/saml.py`,
`outposts/alone.py`, `builtins.py`, `registry.py`, `netops/tls.py`, among others). A third production value
could only be added by finding every copy, and a copy missed would let it through silently.

Two rules assume more than one person. ADR 0029 §3: "An approval needs a fresh authentication and a person other
than the proposer." `_one_of_its_approvers` refuses whoever proposed an action or owns the agent that proposed
it; `services/agent_releases.py` refuses a release's requester and its revision's author; an approver group
(ADR 0196 §1) and a human step addressed to a group (`decider_verdict`) read identity-provider groups, which a
session opened without an identity provider does not have; an exclusion (ADR 0196 §2) holds "whatever their
rank". A person alone, whose agents propose everything, could approve nothing. And the only sign-in is OIDC
(D11), or the development sign-in that production refuses.

## Decision

1. **`personal` is a production environment, and production is named in one place.** `TRANIBOR_ENV=personal` is
   in `PRODUCTION_ENVIRONMENTS` (`packages/core/src/tranibor_core/production.py`), and every module asks
   `is_production()` instead of keeping its own set. Every refusal staging and prod carry applies unchanged: the
   development sign-in (`Settings._no_back_door_in_production`), the fakes (`refuse_fakes_in_production`), the
   demonstration connectors and the `direct` gateway (`builtins.py`, ADR 0109 §1), a PostgreSQL superuser
   (`main.py`), clear Temporal payloads (`refuse_clear_in_production`, ADR 0220) and an unisolated agent
   (`unisolated_agent_refusal`, ADR 0222). `is_personal()` adds the rules below; it removes none. No second
   variable says production. Held by `tests/architecture/test_production_is_named_once.py`, which walks the
   sources of `apps/`, `packages/`, `plugins/` and `tools/` and fails on a set, list or tuple literal holding
   exactly `staging` and `prod` outside `production.py`; by `packages/core/tests/test_production.py` (a personal
   installation is production, and the fakes are refused there) and by
   `apps/api/tests/test_authentication_security.py` (the development sign-in refused under `personal`). S87-09
   has landed. The chart does not offer the posture: `templates/guard.yaml` refuses
   `global.environment: personal` (`tests/charts/test_personal_is_not_a_chart_environment.py`).
2. **One person, held by the database.** At its first start the API writes a single `installation` row recording
   the posture. A `BEFORE INSERT OR UPDATE` trigger on `memberships`, `choregos_sole_member()`, written as the
   audit chain's trigger is (migration `20261117_2300_audit_chain.py`), refuses, when that row says `personal`,
   a membership for a second person, whatever path writes it: the members routes (`routers/admin.py`), a sign-in
   (`routers/auth.py`), a project's creation (`routers/projects.py`), `bootstrap.py` or a raw `INSERT`. A
   machine account (a user with `is_service`, ADR 0131) is not a person and is accepted. The API refuses to
   start when `TRANIBOR_ENV` and the recorded posture differ, either way; when more than one person holds a
   membership; when `TRANIBOR_BOOTSTRAP_ADMINS` names more than one address; and on SQLite. The suite's and a
   developer's SQLite database carries the rule too, as triggers `create_all()` lays the way the migration lays
   them (`db/models/installation.py`), but SQLite is a development database that no migration follows: a
   personal installation's database is PostgreSQL (`refuse_what_the_settings_forbid`,
   `apps/api/src/tranibor_api/personal_installation.py`). Held by
   `apps/api/tests/test_personal_single_member_postgres.py`, on PostgreSQL under the application role: a second
   member refused through the route with a message naming this record, a raw `INSERT` refused by the trigger, a
   machine account accepted, each start-up refusal; and by `apps/api/tests/test_personal_single_member.py`
   (SQLite's triggers compared with the migration's; the posture refused on SQLite).
3. **The sole member decides alone, always freshly signed in, and it is recorded.**
   `sole_member(session, org_id)` (`tranibor_api/personal_installation.py`) is true only when the installation
   row says `personal` and exactly one person holds a membership. Then that person meets what a rule asks of other
   people: separation of duties (`_one_of_its_approvers`, the release approval of `services/agent_releases.py`),
   an approver group, a human step addressed to a group (`decider_verdict` answers `ok`, so the step is listed
   by `GET /me/pending`), an exclusion of ADR 0196 §2, and an approver count above one. Never without a fresh
   authentication: no older than the rule's `step_up_minutes`, or 10 minutes when it names none, a step's answer
   included; a stale one gets the usual 401 `step_up_required`. The decision entry and its audit row carry
   `sole_member: true` and what the rule asked. With two persons, or outside the posture, the same case is
   refused with today's message. A decision still needs a person's session: neither a token nor an MCP client
   decides (ADR 0029 §5). Held by `apps/api/tests/test_sole_member_decides.py`, parametrised over `personal` and
   `prod`: an action proposed by the member's own agent (refused in prod; accepted in personal with a fresh
   authentication, refused with a stale one), a group-addressed step of `default-request` listed and answered,
   an excluded member, the audit row's `sole_member`. The rule of phase C that a runner's owner does not approve
   what their runner proposed (ADR 0234, planned; S87-84) applies outside this posture only, and the two are
   tested together.
4. **A sign-in without a password, by door.** An installation has two doors, the loopback door and the relay
   door, told apart by the edge's listener (6), never by the `Host` header, which a client through the relay
   chooses. `GET /auth/methods` names the three methods below and no other; in this posture the OIDC routes
   (`/auth/login`, `/auth/callback`) answer 404, and outside it every route of the three answers 404.
   - *The owner key, on the loopback door.* `choregos local init` generates an Ed25519 key pair in the
     installation's data directory, the private half at mode 0600. The API holds only the public half
     (`TRANIBOR_OWNER_PUBLIC_KEY_FILE`); no platform service mounts the private half. `choregos local open` (and
     the desktop app, ADR 0229) signs a ticket (audience `choregos-owner`, valid 60 seconds at most, a
     single-use `jti`) and opens the console's loopback URL with the ticket in the fragment, which no server
     receives or logs. `POST /auth/owner` exchanges it, on the loopback door only, for a session of the
     installation's one person. Held by `apps/api/tests/test_owner_sign_in.py` (a ticket replayed, expired,
     signed by another key or presented on the relay door is refused).
   - *A phone, paired from the computer.* The owner, on the loopback door, authenticated no more than 10 minutes
     ago, creates a pairing (`POST /me/device-pairings`): a device authorization already approved,
     `client_kind: browser`, valid 5 minutes, whose code travels in the fragment of
     `https://<relay host>/pair#…`, shown as a QR code. `POST /auth/device/session` exchanges it once (a
     conditional update; the loser of a race is refused), on the relay door only, for a session listed under My
     account and ended there (ADR 0130), audited `device.paired` with the client's name and address. Held by
     `apps/api/tests/test_device_pairing.py`.
   - *Passkeys.* WebAuthn, the relay hostname as RP ID, user verification required, registered from a session.
     An assertion opens a session whose authentication time is now, or re-authenticates the current one; either
     counts as fresh for ADR 0170 (`approval_freshness_s`, `check_gesture`) and for (3). A foreign origin and a
     regressed signature counter are refused; revoking a passkey ends the sessions it opened. Held by
     `apps/api/tests/test_passkeys.py`, with a software authenticator.

   A step-up answer names the method of the door it was asked on: a new owner ticket on the loopback door, a
   passkey on the relay door (`freshness.REAUTH` names the OIDC re-authentication only today).
5. **Cookies differ per door.** On the loopback door (`http://127.0.0.1`) the session cookie is
   `choregos_session` without `Secure`; on the relay door it is `__Host-choregos_session` with `Secure`. A
   cookie is accepted only on the door that issued it. `redirection_sure` (`routers/auth.py`) and
   `allowed_origins` (`origin_guard.py`) accept the loopback origin (`TRANIBOR_LOOPBACK_URL`) beside the public
   URL. Held by `apps/api/tests/test_owner_sign_in.py` (the cookie's flags per door; a loopback cookie refused
   on the relay door).
6. **The edge names the door by its listener.** In `deploy/personal/Caddyfile` the loopback listener is
   published on `127.0.0.1` only and also answers on the internal `runs` network, where the person's runner
   containers reach it by the edge's service name (the run door, ADR 0144, ADR 0226 §12). The relay listener is
   published nowhere; only the relay agent reaches it (ADR 0230). Each listener sets `X-Choregos-Door`
   (`loopback` or `relay`), replacing whatever the client sent. The API is published nowhere and is not on the
   `runs` network, so only the edge and the platform's own containers reach it; in this posture a request
   without a door is refused (421), the health routes excepted. `/outposts/*` (the door of ADR 0148, mounted at
   the root, which the chart's `customHostApiPaths` does not list) and `/api/v1/internal/*` are routed on the
   loopback listener only: on the relay listener the edge answers 404, and so does the API for a request whose
   door is `relay`. Webhook paths (phase B) are public on the relay listener. Held by
   `tests/deploy/test_personal_compose.py::test_the_edge_routes_per_listener` and
   `apps/api/tests/test_owner_sign_in.py` (421 without a door; each door's routes).
7. **The bundle: `deploy/personal/compose.yaml`, on published images.** Only the release's `tranibor-*` images
   by version tag and third-party images pinned by digest; no `build:`. `TRANIBOR_ENV=personal` on every
   platform service. Only the edge (Caddy) publishes a port, on `127.0.0.1`. PostgreSQL keeps `fsync`. Backups
   run nightly and before every upgrade (S87-30) and hold no secret, as ADR 0184 §6 says of its own. Every
   secret comes from `secrets.env`, which `choregos local init` generates at mode 0600: the session secret, the
   database passwords, `TRANIBOR_SECRETS_KEKS`, the Temporal codec keys, the run-token key pair, the outposts'
   door signing key (`TRANIBOR_OUTPOSTS_DOOR_SIGNING_KEY`) and the VAPID keys (ADR 0194). `TRANIBOR_OUTPOSTS` is
   on; `TRANIBOR_RUN_DOOR_HOSTS` names `127.0.0.1`, `localhost` and the edge's name on the `runs` network, under
   `TRANIBOR_INTERNAL_API_GUARD=enforce`; the orchestrator's executor is `outpost` alone (ADR 0226 §6); the
   `runs` network is internal and reaches outside through the egress proxy only. No platform service mounts the
   Docker socket, and none can be given a subscription variable. A person without the CLI starts the `runner`
   profile, whose one service is their own runner (`choregos-outpost`): the only container that mounts the
   socket, the person's and not the platform's, on the `runs` network. Held by
   `tests/deploy/test_personal_compose.py` (`test_no_platform_service_mounts_the_docker_socket`,
   `test_only_the_edge_publishes_and_on_loopback`, `test_postgres_keeps_fsync`, `test_no_literal_secret`,
   `test_no_service_can_receive_a_subscription_credential`), and end to end by the CI job `personal-install`
   (S87-32).
8. **The posture sets defaults, not the rule.** In this posture the effective `sandbox.personal_runner` defaults
   to `placement: required` and `credential: [subscription]`. Whether a run may use a subscription is decided by
   ADR 0227 §2 alone, from the run's place and its work owner, never from the environment's name. The starter is
   seeded into the owner's personal space with the same values (S87-31). Held by
   `apps/orchestrator/tests/test_placement.py` and `apps/api/tests/test_personal_seed.py`.
9. **No conversion, and community edition.** The posture is recorded once and nothing changes it: no route,
   command or migration turns a personal installation into an organisation's, and the start-up check refuses to
   serve its database under another posture. A person who needs a team starts an organisation installation.
   Everything here is in the community edition and needs nothing of the enterprise edition.

## What is refused, and what is only recorded

| Case | Answer | Where |
|:--|:--|:--|
| A second person's membership, on any path | refused | `choregos_sole_member()`; the start-up check |
| A posture mismatch, two persons, two bootstrap addresses, SQLite in the posture | the API refuses to start | `tranibor_api/personal_installation.py` |
| The development sign-in, the fakes, `direct`, a superuser, clear payloads, an unisolated agent | refused, as in prod | `production.py` and its callers |
| An owner ticket replayed, expired, signed by another key, or on the relay door | refused (401; 404 on the relay door) | `POST /auth/owner` |
| A pairing created on the relay door or with a stale sign-in; exchanged twice, late, or on the loopback door | refused | `/me/device-pairings`, `/auth/device/session` |
| A passkey assertion from a foreign origin or with a regressed counter | refused | `passkeys.py` |
| A cookie on the other door; a request without a door | refused (401; 421) | the API's door check |
| `/outposts/*` or `/api/v1/internal/*` through the relay | 404, at the edge and at the API | `Caddyfile`, the API |
| A sole-member decision with a stale sign-in | 401 `step_up_required` | `decide`, the release and step routes |
| A platform service with the Docker socket, a port off loopback, `fsync=off` | refused by a test | `tests/deploy/test_personal_compose.py` |
| `global.environment: personal` | refused at render | `charts/choregos/templates/guard.yaml` |
| A decision the sole member takes alone | recorded: `sole_member: true` and what the rule asked | the decision entry, the audit row |
| A paired phone | recorded: `device.paired`, listed under My account | the audit log, `GET /me/sessions` |

Recorded, and not prevented:

- **The machine's account is the owner.** Whoever runs as the owner's OS user, a person or malware, can read the
  owner key, `secrets.env` and the database's volume, and sign a ticket; the person's runner holds the Docker
  socket with the same reach. The installation's guide (`docs/personal.md`, to be written with the bundle,
  S87-27) says so. It is not mitigated.
- **Separation of duties is gone, on purpose.** One person proposes, through their agents, and approves. A fresh
  authentication proves the person was there, not that a second person looked. Every such decision says so.
- **Recovery is the person's.** A lost machine is a restore from a backup plus the data directory (`secrets.env`
  and the owner key) kept elsewhere; a backup holds neither. Without both, the sealed secrets and the Temporal
  histories are lost, as ADR 0184 §6 and ADR 0225 say for any installation.
- **A passkey is bound to the relay's hostname.** A person who changes relays registers their passkeys again.

## Amended, for this posture only

- **[ADR 0184](0184-a-supported-single-node-installation.md) §1**: "No second chart, no compose file hardened
  into a product." For one person's own installation a compose file on published images is the product (7); for
  an organisation, §1 stands.
- **[ADR 0029](0029-a-platform-for-governed-agents.md) §3**: "An approval needs a fresh authentication and a
  person other than the proposer." The sole member meets the second condition alone; the first is kept and the
  decision records it (3).
- **[ADR 0196](0196-an-actions-approvers-come-from-its-work-item.md) §1**: "The rank is still asked: the group
  is a second condition". The sole member meets an approver group's condition alone (3).
- **ADR 0196 §2**: "An excluded person may not decide or settle the action, whatever their rank". The sole
  member is the exception, with a fresh authentication and `sole_member: true` recorded (3).
- **Plan decisions D9 and D11** keep their values ("Kubernetes, Helm umbrella `charts/choregos`, GitOps Argo CD
  app-of-apps from `choregos-infra`"; "OIDC (Keycloak in dev, the organisation's IdP in prod); RBAC with 5
  roles") and gain one clause each: D9, "a personal installation (ADR 0228) is `deploy/personal/compose.yaml` on
  published images"; D11, "a personal installation (ADR 0228) signs its one person in with an owner key on the
  loopback door, phones paired by a one-time link, and passkeys; no OIDC, no password".

## Unchanged, on purpose

- **ADR 0147, "A personal device as an execution place of a shared project's workflow"**: not triggered. In
  phase A a run reaches a runner only from the owner's personal space (ADR 0226 §8), and a personal installation
  holds one person, so none of its projects is shared: the run's rights, the project's and the machine's are one
  person's.
- **ADR 0061 §3**: a personal agent still never runs in a project's workflow.
- **ADR 0154 §6 and ADR 0193 §2**: the console-brokered device flow stays as it is for the terminal, the editor
  and the Android app. `client_kind: browser` is a kind beside `cli` and `editor`, created by the computer only,
  never started from the phone.
- **ADR 0170**: an organisation's `approval_freshness_s` and a gesture's `step_up_minutes` apply as written; an
  owner ticket and a passkey are ways to meet them.
- **The development install**: `compose.yaml` and `tests/dev/test_local_install.py` do not change; it stays
  `dev`.
- **ADR 0184**: the single-node profile stays the installation of an organisation on one machine.

## Alternatives rejected

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| The single-node k3s profile (ADR 0184) | Linux, 8 vCPU and 32 GiB, an OIDC provider on site | a laptop under Docker Desktop or Podman, no identity provider | Kept for organisations |
| `docker compose up` as it is (`TRANIBOR_ENV=dev`) | a developer's machine nobody else reaches | a phone reaches it: development sign-in, `fsync=off`, fakes allowed | **Refused** for this use |
| `TRANIBOR_ENV=prod` and a second variable (`TRANIBOR_INSTALLATION=personal`) | no new value to route | two switches, combinations nobody tests; `production.py` keeps one variable | **Refused** |
| The `local_docker` executor, the socket in the worker | one install, no daemon | the platform can read the person's token and is root on the host | **Refused** (ADR 0227) |
| An embedded identity provider (Keycloak, a passkey-only OIDC provider) | an organisation joins later | one more container and hostname; the two origins still conflict for passkeys | **Refused** |
| Password accounts | familiar | reset flows and a breach surface | **Refused** |
| Pairing by a code the phone starts (RFC 8628) | no computer at hand | phishable: the owner approves an attacker's code | **Refused** here; kept for the Android app |
| The door read from `Host` | one listener | a relay client sends `Host: 127.0.0.1` and reaches the owner sign-in, the outposts' door and the run's API | **Refused** |
| One cookie for both doors | one name | `Secure` on `http://127.0.0.1` is not honoured alike by every WebView, and one door's cookie would open the other | **Refused** |
| Refusing at write, by ADR 0101, what needs two people | the platform stays strict about separation | no action, release or group step could ever be decided by a person alone | **Refused**: decided alone, freshly, recorded |

## Consequences

- Every refusal added to production from now on reaches a personal installation without a line of its own.
- The migrations (the `installation` table and its trigger, the owner tickets, the passkeys) are security paths
  owed a human review.
- In phase A, agent stages run in the owner's personal space: placement names a work owner there only (ADR 0226
  §8). A project the owner creates has its agent stages refused by placement, with its reason, until phase C
  names a responsible person (ADR 0233 and ADR 0234, both planned).
- Stories S87-09 to S87-14, S87-27, S87-28, S87-30, S87-31 and S87-32 build this. The record proves nothing;
  each guarantee above names the test that will hold it.

## Not proved

No personal installation has run. Untried: how WebKit and WebView2 treat a cookie on `http://127.0.0.1`; Caddy's
per-listener routing behind the relay; the bundle's memory at rest under Docker Desktop's default limits; a
Temporal upgrade on an unattended personal installation.

## Amended, 2026-11-27: the module's name, and SQLite (S87-10)

- 2026-11-27: the posture's module is `apps/api/src/tranibor_api/personal_installation.py`, where this record
  first said `tranibor_api/installation.py` (the header, §2, §3 and the table of refusals): the name sets it apart
  from `services/installation.py`, which installs a template's agents. The `installation` table and its SQLite
  triggers are in `db/models/installation.py`.
- 2026-11-27: §2 said SQLite "keeps no trigger". The suite's SQLite database does keep the rule, as triggers
  `create_all()` lays (`apps/api/tests/test_personal_single_member.py` compares them with the migration's); what
  stands is that the API refuses to start a personal installation on SQLite, a development database that no
  migration follows.
