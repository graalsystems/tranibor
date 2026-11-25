# 0224 — A cloud is claimed only by its proof; every executor isolates the agent or is refused in production

- **Status**: proposed, 2026-11-25 (asked by the project lead on 2026-11-25: "Tekton and Azure Container Apps
  do not isolate the agent, and it is not certain the platform works on the other cloud providers";
  written by story S86-01)
- **Concerns**: [ADR 0222](0222-an-agent-does-not-ship-unisolated-in-production-without-a-named-opt-out.md)
  §5 (amended), [ADR 0146](0146-the-runner-and-the-agent-do-not-share-a-container.md) §9,
  `tranibor_core.agent_isolation` (`WHY_NOT_SEPARATE`), the `tekton` and `aca` executors,
  `docs/security.md` (where the agent is isolated), `docs/deployment.md`, stream S86
  ([PLAN-2026-11-25-isolation-everywhere-and-clouds](../plan/PLAN-2026-11-25-isolation-everywhere-and-clouds.md))

## Context

ADR 0222 closed the silent case: in staging and prod, an agent that runs beside the runner's
credentials needs the opt-out named (`none-accepted`). It left two executors there by design. Under
`container`, `tekton` and `aca` refuse every run, because neither starts the agent's own container
(S65-04l). An installation on either therefore runs the agent unisolated, by name, or does not run.
ADR 0222 §5 judged isolating them "not reasonably small" and did not attempt it.

The second gap is wider and was never written down. Every run of the platform has been on k3s or kind:
the operator's `dev` and `prod` (k3s, Cilium, gVisor, bare metal), the demonstration and the nightly
suites (kind with Calico), the single-node proof (k3d, or k3s on a VM). The ACA executor was verified
against a real subscription, alone. No test, page or record names EKS, GKE, AKS or OpenShift as tried.
A read of the chart (the plan's section 0) finds defaults that would stop an install on a managed
cloud before its first run: monitoring CRDs assumed, nothing opened to the Kubernetes API or to
managed services under a default-deny egress, an egress gate that fails every agent pod where no
policy engine enforces, an object store that takes static S3 keys only.

## Decision

1. **Tekton isolates the agent.** Tekton is chosen for what Chains gives: a signed PipelineRun, hence
   a verifiable provenance for an agent's commits. Dropping it as an agent executor would drop that.
   - At once (S86-02): a deployment whose default executor is `tekton` routes an agent step it cannot
     isolate to a second executor (`k8s_job` by default), in the same cluster, as `agent_sandbox`
     already does (ADR 0072). Such a step is not signed by Chains meanwhile.
   - Then (S86-05, S86-06): the PipelineRun carries its Task inline, so no copy in a project's namespace
     can be older than the executor, and the agent stage carries the agent's container as a sidecar.
     `tekton` declares `separate_agent` only with that sidecar, and only after a cluster run.
2. **ACA isolates the agent, or production refuses it.** A two-container job is run on a real
   subscription first (S86-07). If the runner and the launcher share loopback and an `EmptyDir`, and
   the execution ends with the runner, `aca` starts the agent's container and declares
   `separate_agent` (S86-08). If not, staging and prod refuse `aca` **even with** `none-accepted`:
   an executor that cannot isolate where it is meant to be used is not offered there, and ACA stays for
   `dev` and `test`. This amends ADR 0222 §5, which let both executors run in production by opt-out
   indefinitely.
3. **A cluster kind is claimed only by its proof.** `docs/support/clusters.yaml` records, per cluster
   kind (provider, distribution, sandboxed runtime, policy engine, executors), its last green proof.
   The word a reader sees is computed from that record, never written:
   - **supported**: the install proof ran green within the last 30 days;
   - **tested**: it ran green once;
   - **unknown**: it never ran.
   The install proof is one script for any cluster (S86-09): the cluster preflight, the install, the
   isolation probe of S65-04o from the agent's container, one real ticket carried to its end, the
   egress gate and the object store, an evidence archive, then the cluster destroyed whatever the
   outcome.
4. **The order of providers.** GKE, EKS and AKS first; one European provider second (Scaleway Kapsule or
   OVHcloud MKS, chosen when its turn comes); OpenShift third. Each starts `unknown`.
5. **What the chart assumes becomes a setting, and the cluster is asked before the first run.**
   `tranibor cluster check` (S86-03) asks the cluster what the chart can only assume; the chart's
   hard-coded assumptions (DNS namespace, monitoring CRDs, ingress TLS, the door's annotations, the
   External Secrets version, `kubeVersion`) become settings or follow the cluster (S86-16), and the
   object store takes a cloud identity (S86-17).

## Consequences

- The table "Where the agent is isolated" in `docs/security.md` changes only when a story's cluster run
  proves a row; a row never reads "isolated" on a unit test alone.
- An ACA installation in staging or prod may stop running after S86-08 if the spike is red. The
  refusal names this record and the executors that isolate.
- Proving a provider costs money and accounts: one account per provider with a spending cap, the
  credentials in the CI's secret store and scoped to one resource group or project, never in the
  repository nor in a runner.
- `docs/deployment.md` stops being the place where a reader infers support from prose: it shows the
  generated section.

## Rejected alternatives

- **Retire `tekton` and `aca` as agent executors.** Simplest, and it ends the opt-out case. Rejected for
  Tekton because Chains' provenance is a reason to choose it; for ACA, the spike decides, and the
  refusal in production is this alternative applied where the spike says so.
- **Keep the named opt-out as the answer.** It is honest, but it makes "isolated by default" false on
  two executors for as long as nobody needs them, which is exactly the state that was asked to end.
- **Declare GKE, EKS and AKS supported because Kubernetes is Kubernetes.** The read of the chart says
  otherwise; and "nothing is declared done without a test that fails in its absence" applies to a
  support claim as to a feature.
