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

What the chart and the code assume about a cluster, and where each assumption was run. Paths are under
`charts/choregos/` unless they start elsewhere.

| What | Where | Today | Run on |
|:--|:--|:--|:--|
| The agent-sandbox operator | `charts/orchestrator/templates/sandbox.yaml:32-33`, `infra/operators/agent-sandbox/kustomization.yaml:16-20` | Required by the default executor (`agent_sandbox`), pinned at v1.0.6; the chart refuses to render without its CRDs | The operator's dev and prod only; the nightly kind job does not install it, so its cluster tests skip in CI |
| Native sidecars | `docs/security.md:105-108`, `packages/core/src/tranibor_core/hosted.py:56-69` | Kubernetes 1.29+ for the default `container`; **no `kubeVersion`** in any `Chart.yaml` | kind 1.34 (CI `hosted-cluster`) |
| A sandboxed runtime | `values.yaml:78-89`, `infra/bootstrap/gvisor.yaml` | Opt-in (`global.runner.runtimeClass`); the bootstrap installs `runsc` through a privileged hostPath DaemonSet | dev and prod (gVisor), kind with runsc in CI |
| Landlock | `packages/runner/src/tranibor_runner/confine.py:63-67` | ABI 4 for the port rule; `best_effort` by default, so a node kernel without it only journals | Unit tests and the operator's nodes |
| NetworkPolicy, enforced | `templates/networkpolicies.yaml` | Default-deny egress; DNS opened to `kube-system` only (`:30`, `:153`, `:207`, `:243`); dependencies expected in `choregos-data` and siblings | kind with Calico (nightly), k3d (single-node proof), Cilium (dev) |
| What the platform reaches outside the cluster | `values.yaml:1024-1030` (`platformEgress: []`) | Nothing opened for the Kubernetes API, a managed database, the object store, the identity provider or the model gateway: "a platform team opens these"; on dev an out-of-chart Cilium policy did (`docs/plan/BLOCKERS.md:45`) | dev, by hand |
| The egress gate | `values.yaml:871-887`, `docs/deployment.md` "The egress gate" | Every agent pod exits 115 if a direct connection to `1.1.1.1:443` is not refused within 30 s | Cilium 1.16 (10/10 closed), k3s kube-router (handled); **Calico not proved** (`status/S80.md`) |
| Ingress | `values.yaml:747-772`, `charts/api/templates/httproute.yaml` | Gateway API `HTTPRoute` by default; the `ingress` kind has no TLS block; the outposts door's timeouts are nginx annotations | Render only |
| Monitoring | `values.yaml:979-987`, `templates/monitoring.yaml` | `ServiceMonitor` and `PrometheusRule` **on by default, with no CRD check**: an install fails on a cluster without the Prometheus Operator | Render only |
| Secrets | `charts/api/templates/externalsecret.yaml:2` | External Secrets on by default, at `external-secrets.io/v1beta1`, against `ClusterSecretStore/choregos-cluster-store` | dev and prod |
| The object store | `packages/adapters/src/tranibor_adapters/storage/__init__.py:111-175` | S3 with static keys only: no session token, so no IRSA, Pod Identity or Workload Identity; no GCS or Azure Blob client (Azure Blob has no S3 API) | RustFS, MinIO |
| Volumes | `charts/*/templates` | RWO only, default StorageClass; the backup pod mounts every embedded volume beside its owner, so it holds on one node only (`values.yaml:936-938`) | k3s `local-path` |
| Fixed uids | `sandbox.yaml:155`, `charts/postgresql/templates/statefulset.yaml:96`, … | 1000 (runner), 1001 (agent), 70, 65532, 977, 999 | Clusters that allow a fixed uid |
| Images | `.github/workflows/release.yml:181-195` | The platform's images are amd64 and arm64; third-party images not checked for arm64 | amd64 only |

No test, document or record names EKS, GKE, AKS, OpenShift, Scaleway or OVHcloud as tried. An old plan
aimed at "AKS or GKE (1.31+)" (`06-kubernetes-deployment.md`); it was not carried out. S41-16 says it in
its row: "Does not prove: a cloud cluster".

Conclusion: on a managed cloud the chart's defaults would fail before the first run on at least the
monitoring CRDs, the egress opened to the Kubernetes API and the managed services, and, without a
policy engine, the egress gate. These are settings, not redesigns. What only a run can tell is the
gate's timing on each provider's policy engine, the two containers under each provider's sandboxed
runtime, and the operator on a managed cluster.

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
| S86-16 | 3 | M | What the chart assumes about the cluster becomes a setting | S86-03 |
| S86-17 | 3 | M | The object store takes a cloud identity | — |
| S86-11 | 4 | M | GKE: installed, isolated, a run carried to its end | S86-09, S86-10, S86-16, S86-17 |
| S86-12 | 4 | M | EKS: installed, isolated, a run carried to its end | S86-09, S86-10, S86-16, S86-17 |
| S86-13 | 4 | M | AKS: installed, isolated, a run carried to its end | S86-09, S86-10, S86-16, S86-17 |
| S86-14 | 5 | M | A European provider, then OpenShift | S86-11 |
| S86-15 | 5 | S | The proofs run on a schedule and keep the matrix current | S86-04, S86-11 |

```
wave 0  00 plan · 01 ADR
wave 1  02 tekton routes · 03 preflight · 04 matrix record          (independent)
wave 2  05 tekton sidecar (02) · 06 tekton proof (05) · 07 ACA spike
wave 3  08 ACA isolates or is refused (07) · 09 portable proof (03) · 10 profiles (03)
        16 chart assumptions as settings (03) · 17 object store identity
wave 4  11 GKE · 12 EKS · 13 AKS                                    (09, 10, 16, 17; in parallel)
wave 5  14 European provider, OpenShift (11) · 15 schedule (04, 11)
```

S86-02 is the immediate remedy for Tekton: from wave 1, a Tekton installation runs its agent steps
isolated on `k8s_job` in the same cluster, and keeps Tekton for its CI. S86-03 is the immediate
remedy for the clouds: from wave 1, an operator on any cluster learns before the first run what the
cluster lacks, instead of finding it in a hung pod.

### S86-00 · docs(plan): every executor isolates the agent, and the clouds are proved

- This page, stream S86 in `08-backlog.yaml` with one entry per story heading below, a status row per
  story in `status/S86.md`, the index line in `STATUS.md` and `00-index.md`.
- **Accepted when**: `tests/docs/test_plan_s86_in_backlog.py` finds every `### S86-NN ·` heading in
  the backlog, no S86 story the page lacks, and every dependency before its story; red with one
  backlog entry removed.

### S86-01 · docs(adr): 0224, a cloud is claimed only by its proof; Tekton and ACA isolate or are refused

- Records D1 to D4 as the project lead answers them. Amends ADR 0222 §5 (Tekton and ACA "are not
  isolated here") into a time-boxed path: isolated by S86-06 and S86-08, or refused.
- **Accepted when**: the ADR is indexed (`tests/docs/test_adr_index.py`); `docs/security.md`'s
  table and ADR 0222 link to it.

### S86-02 · feat(adapters): a deployment on `tekton` routes an agent step to `k8s_job`

- `with_custom_image_executor` (`registry.py:451`) wraps a `tekton` primary as it wraps
  `agent_sandbox`: under `container`, a step `tekton` cannot isolate goes to the second executor
  (`TRANIBOR_CUSTOM_IMAGE_EXECUTOR`, chart `runner.customImageExecutor`); a CI or `job` step stays
  on Tekton. The chart sets `k8s_job` as the second executor when `executor: tekton` and
  `agentIsolation: container`, unless the values name another.
- What changes for a reader of the provenance: an agent step routed to `k8s_job` is not signed by
  Chains until S86-05.
- **Accepted when**: `packages/adapters/tests/test_routing.py` starts a `container` run on a
  `tekton` primary and finds it on `k8s_job`, red with the wrap removed; a chart test renders the
  second executor for `executor: tekton`; `/readiness` says `agent-isolation: done` for such a
  deployment.

### S86-03 · feat(cli): a cluster preflight, what the chart needs asked of the cluster

- `tranibor cluster check` (and a Helm test hook) asks the cluster, read-only except for two
  throw-away pods it deletes, for what the platform needs and the chart can only assume: the
  Kubernetes version (1.29+ for native sidecars), the agent-sandbox CRDs and their version when
  `executor: agent_sandbox`, the named RuntimeClass, a default StorageClass, the object store reachable
  from the API's namespace, NetworkPolicy **enforced** (a pod denied by a policy is really denied, not
  only accepted by the API server), and the egress gate closed before a new pod's first packet.
- Each check says what failed, why it matters and the value or the provider setting that fixes it;
  a check that does not apply (no `agent_sandbox`, no RuntimeClass) says so.
- **Accepted when**: on kind without a NetworkPolicy engine, the enforcement check fails and names it
  (the test that holds it is red with the check removed); on kind with Calico, it passes; on the dev
  cluster, every check passes (recorded in the status row).

### S86-04 · docs(deployment): the support matrix is a record, and the docs read it

- `docs/support/clusters.yaml`: one entry per cluster kind (provider, version, runtime, network
  policy engine, executor), its last green proof (date, version, run id or log URL) and its word from
  D4 (`supported`, `tested`, `unknown`). `docs/deployment.md` gets a "Where the platform is proved"
  section generated from it (`make docs-support`).
- Seeded with what exists: k3s 1.31 + Cilium + gVisor (operator's dev and prod), kind, k3s
  single-node, the ACA executor alone. Every managed cloud starts `unknown`.
- **Accepted when**: `tests/docs/test_support_matrix.py` refuses a page that calls a provider
  supported without an entry, an entry `supported` whose proof is older than 30 days, and a generated
  section that differs from the record.

### S86-05 · feat(adapters): Tekton's agent stage carries the agent's container

- The executor stops referring to a Pipeline copied into the namespace: the PipelineRun carries its
  `pipelineSpec` inline (clone, then the agent stage), so no older copy can run the agent beside the
  runner (the reason S65-04l gave). Chains signs an inline PipelineRun as it signs one by reference.
- The agent stage gets an `agent` sidecar built from the same values as `k8s_job`'s: uid 1001,
  read-only root, no secret, no `envFrom`, `/workspace` and an `emptyDir` `/exchange` shared,
  `launcher` as its command. With Tekton's `enable-kubernetes-sidecar` (Kubernetes 1.29+), it is a
  native sidecar; without it, Tekton's own sidecar, which the controller waits for by default
  (`await-sidecar-readiness`) and stops after the last step.
- `tekton` declares `separate_agent`; `WHY_NOT_SEPARATE["tekton"]` goes; the chart's reference Task
  ConfigMap goes with the copy it fed.
- **Accepted when**: `packages/adapters/tests/test_tekton.py` renders a PipelineRun with the inline
  spec and the sidecar, and none under `process`; `tests/conformance/executors/test_capabilities.py`
  shows `tekton` declaring the capability; both red with the sidecar removed.

### S86-06 · test(cluster): Tekton's isolation proved on kind with Tekton, then under gVisor

- `tests/cluster/test_agent_isolation.py` gains a `tekton` mode: Tekton Pipelines installed on kind
  (pinned version), the same probe as S65-04o from the agent's sidecar and, as the control, from the
  runner's step. Then the same on a cluster with the `gvisor` RuntimeClass, both sidecar modes.
- **Accepted when**: from the agent: 0 run token, 0 gateway key, 0 runner process; from the runner's
  step: the control finds them; the work crosses `/workspace` and `/exchange` both ways; the
  PipelineRun ends when the step ends (no sidecar left running). One real agent run carried to its
  end, its journal `agent.isolation: container`.

### S86-07 · measure(adapters): a two-container ACA job on a real subscription

- On a throw-away resource group (`docs/runbooks/aca-live.md`), a job whose template lists the
  runner and the launcher. Microsoft's documentation allows several containers in a job ("an advanced
  scenario") and shares an `EmptyDir` between containers of one replica; it does not say when an
  execution with two containers ends, nor whether they share loopback, and exposes no
  `securityContext`.
- Recorded in STATUS: loopback between the two (the three listeners), `EmptyDir` both ways, the uid
  each container runs as (the image's `USER`), what the execution does when the runner ends and the
  launcher does not, and when the launcher exits on its own once the runner disconnects (the launcher
  is ours: it can end itself, whatever ACA does with the second container).
- **Accepted when**: `tests/live/test_aca_live.py` has a `two_containers` test that records the five
  answers, or the STATUS row names each that failed and why. What is built here may be thrown away.

### S86-08 · feat(adapters): ACA starts the agent's container, or is refused in production

- If S86-07 is green: the job template carries the agent's container (agent image with `USER 1001`,
  no secret reference), the launcher ends when the runner disconnects, `aca` declares
  `separate_agent`, and the live suite runs the S65-04o probe from both containers.
- If it is not: `aca` stays refusing `container`, and staging and prod refuse `aca` **even with**
  `none-accepted`, naming this story; `docs/deployment.md` says ACA is for `dev` and `test`.
- **Accepted when**: either the live probe (0 credential from the agent's container, the control
  finds them) is green and recorded, or the refusal in production is tested and red with it removed.
  One test either way.

### S86-09 · test(install): the install proof, made portable — one script, any cluster

- `tools/cluster-proof/proof.sh`, grown from `tools/single-node/proof.sh`: given a kube context and a
  provider profile, it runs the preflight (S86-03), installs the chart, runs the S65-04o isolation
  probe in platform mode, carries one real ticket to its end with the capped model key, checks the
  egress gate and the object store, collects the evidence into one archive and uninstalls.
- Provider-specific creation and destruction live beside it as OpenTofu modules
  (`tools/cluster-proof/<provider>/`), each with a spending tag and a destroy that runs whatever the
  proof's outcome.
- **Accepted when**: the script runs green on kind and on k3d in CI; a failure at any step still
  uninstalls and keeps the evidence; the evidence archive is what S86-04's record cites.

### S86-10 · feat(charts): provider profiles

- `charts/choregos/values/{gke,eks,aks}.yaml`, then the European provider and OpenShift: only what
  differs per provider (RuntimeClass, StorageClass, ingress, object store endpoint, the image
  registry's pull secret), each commented with the provider setting it relies on.
- What each provider offers, from its documentation (to be confirmed by S86-11 to S86-13): GKE
  Sandbox runs gVisor under RuntimeClass `gvisor` (enabled by default on Autopilot) and refuses
  seccomp, AppArmor and hostPath in a sandboxed pod; AKS pod sandboxing runs Kata under
  `kata-vm-isolation` (GA, Azure Linux nodes only), and its network policy engine (Cilium, Calico or
  Azure NPM, the last retired in 2028) can be set on an existing cluster; EKS's VPC CNI enforces
  NetworkPolicy from 1.21.0, and no managed sandboxed runtime was found in its guide.
- **Accepted when**: `tests/charts/` renders each profile and kubeconform accepts it; a profile that
  names a RuntimeClass renders it on the runner and agent pods; a profile cannot turn off
  `agentIsolation: container`.

### S86-16 · fix(charts): what the chart assumes about the cluster becomes a setting

- `Chart.yaml` declares `kubeVersion: ">=1.29.0-0"`. The DNS rule's namespace and port become a
  setting (OpenShift serves DNS from `openshift-dns`; GKE's NodeLocal DNSCache needs an `ipBlock`).
  `ServiceMonitor` and `PrometheusRule` render only when the cluster serves their CRDs, or when an
  offline render says so, as the agent-sandbox check does. The `ingress` kind gets a TLS block and
  the outposts door's timeouts a per-controller form. `platformEgress` gets documented examples for
  the Kubernetes API and the managed services of each provider; the preflight (S86-03) says which
  are missing. The External Secrets API version follows what the cluster serves.
- **Accepted when**: one chart test per item, each red with its change removed; the default values
  still render the operator's dev and prod unchanged (`tests/charts/`).

### S86-17 · feat(adapters): the object store takes a cloud identity

- The S3 client signs with temporary credentials (session token) obtained from the pod's identity:
  EKS IRSA or Pod Identity. Google Cloud Storage speaks S3 only with HMAC keys, so a GCS client with
  Workload Identity, and an Azure Blob client with Azure Workload Identity, go behind the same store
  port (Azure Blob has no S3 API). Static keys stay. The fakes and RustFS stay the default for a bench.
- **Accepted when**: a unit test signs with a session token and is red without the header; the
  Azure Blob store passes the store's conformance tests against Azurite; each cloud identity is proved
  in its provider's story (S86-11 to S86-13).

### S86-11 · test(install): GKE — installed, isolated, a run carried to its end

- S86-09 on a GKE Standard cluster with a GKE Sandbox node pool, then on Autopilot. The question the
  documentation raises first: the chart sets `seccompProfile: RuntimeDefault`, which a sandboxed pod
  does not support.
- **Accepted when**: the proof's archive is green and recorded in `docs/support/clusters.yaml`
  (`tested`); every defect it finds is a fix with its own test, or a finding issue.

### S86-12 · test(install): EKS — installed, isolated, a run carried to its end

- S86-09 on EKS with the VPC CNI's network policy turned on. Without a managed sandboxed runtime,
  the agent is isolated by its own container under runc; gVisor on self-managed nodes is a second,
  optional run. The profile says which one a reader gets.
- **Accepted when**: as S86-11.

### S86-13 · test(install): AKS — installed, isolated, a run carried to its end

- S86-09 on AKS with Azure Linux nodes, `kata-vm-isolation`, and Cilium as the policy engine. The
  questions: the egress gate's timing under Kata, and the two containers of one pod inside one VM.
- **Accepted when**: as S86-11.

### S86-14 · test(install): a European provider, then OpenShift

- The European provider chosen in D3, then OpenShift. On OpenShift the first question is the uid
  split: the chart pins the runner to 1000 and the agent to 1001, and the default SCC assigns uids
  from the namespace's range (to be confirmed: Red Hat's page could not be read from here). If a
  fixed uid is refused, the separation must hold with two uids drawn from the range, or the profile
  names the SCC it needs.
- **Accepted when**: as S86-11, for each.

### S86-15 · ci(install): the proofs run on a schedule and keep the matrix current

- A weekly job per provider runs S86-09 against a fresh cluster and writes its result into
  `docs/support/clusters.yaml` through a pull request; a red run opens a finding issue naming the
  step.
- **Accepted when**: a provider whose last green run is older than 30 days reads `tested`, not
  `supported`, in the generated documentation (S86-04's test); the destroy step is proved by a run
  that fails midway and leaves no cluster.

---

## 4. What every story must keep true

| Invariant | Held by |
|:--|:--|
| No row of `docs/security.md`'s table reads "isolated" for an executor that is not | S86-05, S86-08 and the table's test |
| `process` in staging and prod stays refused unless named (ADR 0222) | unchanged; S86-08 may only make it stricter |
| No provider is named as supported without a recorded proof | S86-04 |
| A proof never leaves a cluster or a bill behind | S86-09, S86-15 |
| No cloud credential in the repository, nor a broad one in a runner | the CI's secret store, per provider, scoped to one resource group or project |

## 5. Effort and dates

| Part | Stories | Effort (person-days) | Waits for |
|:--|:--|:-:|:--|
| Remedies of wave 1 | S86-02, S86-03, S86-04 | 6 to 10 | nothing |
| Tekton isolated | S86-05, S86-06 | 8 to 12 | D1 |
| ACA decided | S86-07, S86-08 | 5 to 9 | an Azure subscription (exists: S13-05) |
| Chart and object store made portable | S86-16, S86-17 | 8 to 14 | nothing |
| Three managed clouds | S86-09 to S86-13 | 20 to 32 | D3, D5 (accounts and budget) |
| European provider, OpenShift, schedule | S86-14, S86-15 | 10 to 16 | S86-11 |

As in the roadmap of 2026-11-17, the real-system proof dominates, not the code. The stream does not
move gate J3 (the first outside installation is one k3s node, already proved); it is what gate J5
("repeatable") needs before an installation on a managed cloud is offered.

## 6. Not decided here

- Whether the hosted offer runs on one of these clouds: a product decision, outside this page.
- A KMS per provider for S4's per-organisation keys: S4's own decision.
