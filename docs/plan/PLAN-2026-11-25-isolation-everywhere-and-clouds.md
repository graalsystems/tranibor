# Plan — 2026-11-25: every executor isolates the agent, and the clouds the platform claims are proved (stream S86)

Two gaps, said plainly on 2026-11-25:

1. **Tekton and Azure Container Apps do not isolate the agent.** Neither starts the agent's own
   container (S65-04l): under `container`, the chart's default, both refuse every run; under
   `process`, staging and prod refuse them unless the operator names the opt-out
   (`none-accepted`, ADR 0222). The opt-out is honest, but it means an installation on either
   executor ships with the agent beside the run's credentials.
2. **No managed cloud has ever run the platform.** Every proof is on k3s or kind: the operator's
   `dev` and `prod` (k3s 1.31, Cilium, bare metal, the `gvisor` RuntimeClass), the demonstration
   and the e2e suite (kind), the single-node proof (k3d or k3s on a VM). The ACA executor was
   verified against a real Azure subscription, alone: no installation of the platform on Azure, and
   nothing on EKS, GKE, AKS or OpenShift. Whether it works there is not known.

Checked against `main` at `d5e2dc9`. This page changes no code. ADR: **0224**, to write in wave 0
(story S86-01). When this page and the code disagree, the code wins and the page is a bug.

Status words: **exists** (in the code), **missing**, **unknown** (only a run on the real system can
say).

---

## 0. What the audit found

### The two executors

| What | Where | Today |
|:--|:--|:--|
| The refusal | `packages/core/src/tranibor_core/agent_isolation.py:89-116` (`WHY_NOT_SEPARATE`), each executor's `start` through `refuse_unless_separate` | **Exists**: both refuse `container` naming themselves; `/readiness` shows the same sentence (S65-04l, `tests/conformance/executors/test_capabilities.py`) |
| The opt-out in production | ADR 0222 §2-4, `global.runner.acceptUnisolatedAgent` | **Exists**: staging and prod refuse `process` unless `none-accepted` is named |
| Tekton's PipelineRun | `packages/adapters/src/tranibor_adapters/executor/tekton.py:227-262` | `pipelineRef` to a Pipeline (`templates/github-tekton-argo-k8s/tekton/pipeline-agent.yaml`) whose `agent-stage` Task is **copied into each project's namespace** (`charts/…/choregos-tools/templates/task.yaml`, "Tekton no longer shares ClusterTasks"); one step, uid 1000, the run token in the step's environment |
| Why Tekton is worth keeping | `pipeline-agent.yaml:1-2` | Tekton Chains signs the PipelineRun, which makes an agent commit's provenance verifiable; no other executor gives that |
| ACA's job | `packages/adapters/src/tranibor_adapters/executor/aca.py:180-240` | `"template": {"containers": [container]}`: one container, the run token as a job secret (`run-token`) |
| ACA's live proof | `tests/live/test_aca_live.py`, `docs/runbooks/aca-live.md` | **Exists** for one container: create, trigger, terminal state, a replayed `start`, the token absent from ARM, cancel, 404, logs (S13-05) |
| Routing a run the default executor cannot isolate | `packages/adapters/src/tranibor_adapters/executor/routing.py:66-78`, `registry.py:451-464` | **Exists** for `agent_sandbox` only: `with_custom_image_executor` wraps no other primary, so a deployment whose default is `tekton` cannot send an agent step to `k8s_job` |
| The launcher, the three loopback listeners, the shared `/workspace` and `/exchange` | ADR 0146, `k8s_job.py`, `local_docker.py`, the warm pool's `SandboxTemplate` | **Exists** and proved on dev and prod (S65-04i, S65-04m, S65-04o): nothing new to invent, only a second container to start |
| Who runs these executors | `choregos-deploy` values for dev and prod | Nobody at the operator: `allowedExecutors: [agent_sandbox, k8s_job]`, "no Tekton on this cluster". Tekton's template and ACA's are for outside installations |

### The clusters

<!-- section 0.2 is written from the inventory of cluster assumptions; see below -->

---

## 1. What could not be decided from the code

| Question | Why the code cannot say | The experiment | Story |
|:--|:--|:--|:--|
| Does a Tekton sidecar start before the runner's step and stop when it ends, with the launcher in it? | Tekton waits for sidecars to be ready and stops them after the last step: that is Tekton's controller, not this code; its interplay with a native sidecar flag and gVisor was never run | A PipelineRun with the `agent` sidecar on kind with Tekton Pipelines installed, then under gVisor | S86-05 |
| Does an ACA job with two containers share loopback and an `EmptyDir`, and end when the runner ends? | ACA's job template is a list of containers; what the platform does with the second was never run (`WHY_NOT_SEPARATE["aca"]`) | The live suite with a two-container job on a throw-away subscription | S86-07 |
| Which uid does an ACA container run as? | ACA exposes no `securityContext`: the uid is the image's `USER` | The same live run, `id` from both containers | S86-07 |
| Which sandboxed runtime does each managed cloud offer, under which RuntimeClass name? | Provider-side; changes with their releases | A cluster of each, the RuntimeClass listed and a pod run under it | S86-11 to S86-14 |
| Does the egress gate close on each provider's network policy engine before the agent's first packet? | Measured on k3s (kube-router) and Cilium only (`docs/deployment.md`, "The egress gate") | The gate's probe on each provider | S86-11 to S86-14 |
| Does the uid split (runner 1000, agent 1001) survive OpenShift's `restricted-v2` SCC, which assigns a random uid per namespace? | The chart pins both uids | A namespace under the default SCC, the chart installed as is | S86-14 |

---

## 2. Decisions

| # | Decision | Recommendation | Who |
|:--|:--|:--|:--|
| D1 | Tekton: isolate, or retire it as an agent executor | **Isolate** with an inline Task (no copy in the namespaces), because Chains' provenance is the reason to choose Tekton; route to `k8s_job` meanwhile (S86-02) | project lead |
| D2 | ACA: isolate, or keep it for `dev`/`test` only | **Isolate if the live spike is green**; otherwise ACA is documented as unfit for production and staging and prod refuse it even with the opt-out | project lead, after S86-07 |
| D3 | Which managed clouds the platform claims | **GKE, EKS and AKS first, one European provider (Scaleway Kapsule or OVHcloud MKS) second, OpenShift third**. Nothing is called supported before its proof is green | project lead |
| D4 | What "supported" means | A provider is **supported** when its install proof ran green in the last 30 days, **tested** when it ran green once, **unknown** otherwise; the documentation reads the record, never prose | ADR 0224 |
| D5 | Cloud accounts and budget | One account per provider, a spending cap, credentials in the CI's secret store, every cluster destroyed at the end of its run | project lead (action) |

---

## 3. Stories and order

| Story | Wave | Size | Title | Depends on |
|:--|:-:|:-:|:--|:--|
| S86-00 | 0 | S | This plan, the backlog and the status rows | — |
| S86-01 | 0 | S | ADR 0224: a cloud is claimed only by its proof; Tekton and ACA isolate or are refused | — |
| S86-02 | 1 | S | A deployment on `tekton` routes an agent step to `k8s_job` | — |
| S86-03 | 1 | M | A cluster preflight: what the chart needs, asked of the cluster | — |
| S86-04 | 1 | S | The support matrix is a record, and the docs read it | S86-01 |
| S86-05 | 2 | M | Tekton's agent stage carries the agent's container | S86-02 |
| S86-06 | 2 | M | Tekton's isolation proved on kind with Tekton, then under gVisor | S86-05 |
| S86-07 | 2 | M | Measure: a two-container ACA job on a real subscription | — |
| S86-08 | 3 | M | ACA starts the agent's container, or is refused in production | S86-07 |
| S86-09 | 3 | M | The install proof, made portable: one script, any cluster | S86-03 |
| S86-10 | 3 | S | Provider profiles of the chart | S86-03 |
| S86-11 | 4 | M | GKE: installed, isolated, a run carried to its end | S86-09, S86-10 |
| S86-12 | 4 | M | EKS: installed, isolated, a run carried to its end | S86-09, S86-10 |
| S86-13 | 4 | M | AKS: installed, isolated, a run carried to its end | S86-09, S86-10 |
| S86-14 | 5 | M | A European provider, then OpenShift | S86-11 |
| S86-15 | 5 | S | The proofs run on a schedule and keep the matrix current | S86-04, S86-11 |

```
wave 0  00 plan · 01 ADR
wave 1  02 tekton routes · 03 preflight · 04 matrix record          (independent)
wave 2  05 tekton sidecar (02) · 06 tekton proof (05) · 07 ACA spike
wave 3  08 ACA isolates or is refused (07) · 09 portable proof (03) · 10 profiles (03)
wave 4  11 GKE · 12 EKS · 13 AKS                                    (09, 10; in parallel)
wave 5  14 European provider, OpenShift (11) · 15 schedule (04, 11)
```

S86-02 is the immediate remedy for Tekton: from wave 1, a Tekton installation runs its agent steps
isolated on `k8s_job` in the same cluster, and keeps Tekton for its CI. S86-03 is the immediate
remedy for the clouds: from wave 1, an operator on any cluster learns before the first run what the
cluster lacks, instead of finding it in a hung pod.

<!-- story blocks follow -->
