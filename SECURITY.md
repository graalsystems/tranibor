# Security policy

## Reporting a vulnerability

Write to **security@graal.systems**, or open a private security advisory on GitHub. Please do
not open a public issue: we would rather fix before we publish.

Include the version or commit, what you observed, how to reproduce it, and the impact you
estimate. We acknowledge within 3 working days and give a first assessment within 10.

## Supported versions

The project is at **0.x**: only the latest minor receives security fixes, and there is no
long-term support branch yet. A supported-version table will mean something once a 1.0 exists;
until then, upgrading is the fix.

## What we consider a vulnerability

- Any way for an agent to write outside its scope without being caught.
- Any secret leak: into a workspace, an image, a log, a transcript.
- Any bypass of the three production locks (merge queue, release train, guardrails).
- Any privilege escalation into the cluster from a runner.
- Any access to another organisation's data (RLS, RBAC, audit).

## What we do not consider a vulnerability

- An agent producing bad code: that is what CI and review are for.
- A model following an injected instruction **without that granting it any power**: the
  mechanisms (scope, absence of credentials, gates) apply just the same.
- `TRANIBOR_FAKES=1`, which is not meant for production.

## What we do, and where to check it

| Claim | Where it is enforced |
| :-- | :-- |
| Images are signed (cosign keyless) and admitted by digest | `.github/workflows/release.yml`, `infra/policies/images.yaml` |
| An SBOM and a `mode=max` provenance attestation ship with every release image | `release.yml`, job `images` |
| A CRITICAL from Trivy blocks publication — on `main` **and** on the released image | `ci.yml` job `images`, `release.yml` job `manifest` (`exit-code: 1`) |
| The nightly dependency scan **blocks**, and its failure opens an issue | `nightly.yml` (`exit-code: 1`, job `alerte`) |
| Secrets never enter git: gitleaks is a gate on every pull request | `ci.yml` job `secrets` |
| Organisations are isolated by fail-closed PostgreSQL RLS, audit included | migrations `b2d4f6a8c0e1`, `c3e5a7f9b1d4`, `db/session.py` |
| The audit log is an append-only hash chain per organisation, with signed checkpoints and an optional export to a SIEM (ADR 0182, ADR 0207) | migration `20261117_2300_audit_chain.py`, `audit_chain.py`, `audit_export/`, `apps/api/tests/test_audit_chain.py`, `apps/api/tests/test_audit_export.py` |
| Secrets rotate on the schedule in `docs/runbooks/secret-rotation.md` | that runbook |
| A run that ended stays ended: the orchestrator closes the row before it asks the executor to cancel, and a closed run's API refuses everything but its journals (ADR 0143) | `tranibor_core/run_closure.py`, `activities/execution.py`, `deps.py` (`OpenRunAuth`), `apps/api/tests/test_run_closed.py`, `apps/orchestrator/tests/test_run_closed_when_the_wait_ends.py` |

## What we do **not** guarantee, stated rather than discovered

- **Runner guardrails are a net, not a wall.** What they cannot classify is allowed and logged,
  unless the project policy sets `sandbox.unknown_requests: reject`. The hard mechanisms are the
  sandbox (NetworkPolicy, per-project egress proxy, non-root, no service-account token) and the
  after-the-fact diff check.
- **The development login** (`/auth/login?as=`) is off by default and refused in staging and
  production, by the API *and* by the chart. An installation that turns it on is open to anyone
  who can reach the API.
- **RLS assumes the API does not connect as a PostgreSQL superuser** — a superuser ignores every
  policy. The API refuses to start that way in staging and production.
- **Platform administration is a right on the instance, and in a multi-organisation installation
  nobody holds it by default.** Fixed on 2026-09-27: `require_platform_admin` demands to be
  `org_admin` of **every** organisation of the instance, so the administrator of one tenant no longer
  reaches `/platform/*` (backends, executors, model profiles, gateway keys). With a single
  organisation, its administrator is the instance's. With two or more, the right is granted to no one
  until an installed extension declares a platform administrator (`edition.declare_platform_administrator`):
  a refusal, by design. `Principal.is_platform_admin()` still exists and still answers true for any
  `org_admin`, so no route may use it (`apps/api/tests/test_edition.py`,
  `test_administering_the_platform_is_a_right_on_the_instance`; `deps.py`).
- **A run token cannot be revoked** before it expires (`max_minutes + 15`). What it opens ends with
  the run instead (ADR 0143): once a run is closed — its result is recorded, or the orchestrator
  stopped waiting for it — its token still appends to the run's two journals (`events`,
  `network/decisions`) and gets the idempotent answer of `POST …/result`, which changes nothing;
  every other route of the run's API answers 409 `run_closed`. Closing a run does not reach what
  was minted before: the run's git token lives its hour (`contents: write` on the run's repository),
  the egress proxy serves the network it cached for up to a minute, and a tunnel already open stays
  open. The running run of an interpreter that died is not closed (a restart awaits it again), and a
  Temporal termination closes nothing.
- **The audit log is tamper-evident, not tamper-proof, and keeps no retention rule.** Since ADR 0182
  each organisation's entries, and the platform's, form a hash chain that PostgreSQL links in a trigger;
  `UPDATE` and `TRUNCATE` are revoked and refused by a trigger, and `choregos-admin audit verify` reports
  the first break. But the API's database role owns the tables: with DDL it could grant itself `UPDATE`
  again or drop a trigger, and a superuser can do anything. What reaches past that hand is a copy kept
  elsewhere: a checkpoint signed with a key the database does not hold (`choregos-admin audit checkpoint`,
  run by an operator, not on a schedule), or the SIEM export of ADR 0207 (off by default), which narrows
  the window to the entries the sink has not yet received. History written before the chain was installed
  is chained from that point only. No code deletes audit entries other than an organisation's own
  deletion, and no configurable retention rule exists yet (`docs/security.md`, "The audit log as evidence"; tests
  `apps/api/tests/test_audit_chain.py`, `test_audit_chain_postgres.py`, `test_audit_export.py`).

This section is deliberately longer than the one above it. Where a page and the code disagree,
the code wins — and the page is a bug.
