# 0222 — An agent does not run beside the runner's credentials in staging or prod without a named opt-out

- **Status**: accepted, 2026-11-24 (finding from a buyer's review: "agent isolation is uneven by executor")
- **Concerns**: `tranibor_core/agent_isolation.py` (`unisolated_agent_refusal`), `executor/isolation.py`
  (`refuse_unless_separate`), `StageJobSpec.environment` and `agent_isolation_accepted`, the
  orchestrator's `agent_isolation` setting and `activities/execution.py`, the runner's `_isolate`,
  `/readiness` (`agent-isolation`), `compose.yaml`, the chart's `global.runner.acceptUnisolatedAgent`
  and `choregos.agentIsolation`; completes [ADR 0146](0146-the-runner-and-the-agent-do-not-share-a-container.md)
  §9 and its S65-04l decision; leaves [ADR 0094](0094-the-kernel-confines-the-agent-at-launch.md) as it is

## Context

ADR 0146 made the chart run the agent in a container of its own by default (0.27.17). Three places still
ran the agent in the runner's container, where it can read the run token, the git token and the egress
report key, and said so quietly:

- **Docker Compose** (`docker compose up`, the local install): the `local_docker` executor *can* start the
  agent's container (S65-04h), but `compose.yaml` never asked, because the code's default is `process`.
  Worse, its default runner image was `0.17.12`, which predates the launcher: an image that old ignores
  `TRANIBOR_RUNNER_AGENT_ISOLATION` and would run the agent as its child while the executor started a
  launcher beside it, without a word.
- **Tekton and Azure Container Apps**: neither starts the agent's container (S65-04l). Under `container`
  they refuse every run, so a deployment on them set `process`: a choice, journaled as `agent.isolation:
  process`, but one that nothing in staging or prod ever asked a human to make by name.
- **Any process started without the chart**: the code's default is `process`.

gVisor is not set by default either.

The plan's line is that nothing ships with the agent not isolated.

## Decision

1. **Compose isolates.** `x-platform-env` carries `TRANIBOR_RUNNER_AGENT_ISOLATION: container` for the API
   (`/readiness`) and the workers, whose `local_docker` executor starts the agent's container. The default
   `TRANIBOR_RUNNER_VERSION` moves from `0.17.12` to `0.29.3`, the latest release; a test refuses a default
   older than `0.27.17` (the first image that carries the launcher and the Docker side) and a disagreement
   between `compose.yaml` and `.env.example`. Nothing else in Compose could serve it: `local_docker` already
   declared `separate_agent`, so the missing piece was the setting and an image that honours it.
2. **In `staging` and `prod`, `process` is refused unless named.** One function, `unisolated_agent_refusal`,
   judges `TRANIBOR_ENV` (`is_production`), the isolation the run asks for and the opt-out
   `TRANIBOR_AGENT_ISOLATION=none-accepted`. Every executor's `start` calls it first, through
   `refuse_unless_separate`, before anything reaches its backend (`k8s_job`, `agent_sandbox`, `local_docker`,
   `tekton`, `aca`: tested for each); the refusal is an `AgentIsolationRefusedError`
   (`agent_isolation_unavailable`, not retried) that names `TRANIBOR_AGENT_ISOLATION=none-accepted`, the
   chart's `global.runner.acceptUnisolatedAgent` and the way to isolate. The orchestrator hands each
   run's `StageJobSpec` its `environment` and whether the opt-out is set; the runner repeats the judgment on
   its own environment (exit 100, `run.failed` with `phase: isolation`) so that a runner started by a
   hand-made Job or a stale template refuses too, and the orchestrator passes the opt-out in the runner's
   environment beside `TRANIBOR_ENV`, so the two always travel together. Dev, test and the fakes are not
   judged (the fake executor starts no runner; `TRANIBOR_FAKES` is refused in production anyway), and
   neither is a hosted session's pod, which starts no agent and leaves `environment` empty.
3. **The opt-out is named, logged and visible.** The only accepted value is `none-accepted`; any other word
   is refused at the orchestrator's start (a typo never reads as an acceptance, as for
   `TRANIBOR_RUNNER_AGENT_ISOLATION`). Each run that uses it logs `agent_isolation.none_accepted` (warning,
   with run, project, executor, environment), the runner journals `agent.isolation` with
   `accepted: none-accepted`, and `/readiness` shows `agent-isolation` as `check` (not `done`), or `todo`
   when it is not set and every run would be refused.
4. **The chart makes the choice at render time.** `global.runner.agentIsolation: process` with
   `global.environment: staging|prod` fails the render unless `global.runner.acceptUnisolatedAgent: true`,
   which renders `CHOREGOS_AGENT_ISOLATION=none-accepted` (adopted as `TRANIBOR_AGENT_ISOLATION`) in every
   pod that reads the common environment. Under `container` the value is ignored and not rendered.
5. **Tekton and ACA stay refusing under `container`; they are not isolated here** (amended by
   [ADR 0224](0224-a-cloud-is-claimed-only-by-its-proof-every-executor-isolates-or-is-refused.md): Tekton isolates;
   ACA isolates or is refused in production). Isolating Tekton would
   need the agent as a second step or sidecar of the Task, sharing only the workspace and exchange volumes,
   as another uid, started by a launcher the runner step reaches on the pod's loopback. A Tekton `Task` is
   copied into each project's namespace, so every namespace's copy must be updated and an older copy
   refused before it runs the agent beside the runner (S65-04l's reason), and Tekton's sidecar lifetime
   (sidecars are stopped when the steps end, not ordered before them) must be shown to give the runner a
   ready launcher. ACA's job runs one container per execution here and what it does with a second one was
   never run. Neither is "reasonably small", so neither is attempted: the operator accepts `process` by
   name, or moves the project to `k8s_job`, `local_docker` or `agent_sandbox`.
6. **gVisor stays an opt-in, with a clear recipe.** The chart does not default `runtimeClassName: gvisor`
   by looking the RuntimeClass up: `lookup` returns nothing under `helm template` and Argo CD's offline
   render, so the same values would render differently by renderer; a pod naming a RuntimeClass the
   cluster lacks is never admitted, so a wrong guess hangs every run, and the cluster is where the
   decision is known; and Landlock does not exist under gVisor (ADR 0094), so it is a trade, not a free
   hardening. `global.runner.runtimeClass: gvisor` is the whole change (values comment, `docs/security.md`,
   `docs/deployment.md`), and a chart test holds that the default names no class and that no template
   looks a RuntimeClass up.

## Consequences

- A deployment in staging or prod on `tekton` or `aca`, or on any executor under `process`, stops running
  until it sets `acceptUnisolatedAgent: true` (or `TRANIBOR_AGENT_ISOLATION=none-accepted`) or isolates.
  The chart refuses at render; a non-chart deployment is refused at the first run, with the setting named.
- `docker compose up` runs each agent isolated and needs the 0.29.3 runner image (about a gigabyte, pulled
  on the first run). A user who pins `TRANIBOR_RUNNER_VERSION` below 0.27.17 gets no isolation and no
  message from an image that cannot say it; the compose file's comment and the test pin the default only.
- The guarantee table says what holds per executor and per environment; no row reads "isolated" for a
  mode that is not.

## Not proved

No `docker compose up` with a real runner image (no Docker daemon here: the shape of the second container
is proved by ADR 0146's tests and the `local_docker` tests, not by a compose run); no staging or prod
cluster run of the refusal; gVisor on Compose (Docker's `--runtime=runsc` is not set).
