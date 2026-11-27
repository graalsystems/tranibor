#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The install proof on ANY cluster (S87-09, ADR 0227 §3): the single-node proof's checks, made portable.
#
#   PROOF_CONTEXT=<kube context> tools/cluster-proof/proof.sh [extra values files...]
#
#   the cluster preflight (when the CLI has it) -> the chart with values/all-in-one.yaml, then the
#   provider profile and the files given -> an agent pod reaches what a run uses and not the database,
#   Temporal or the object store -> the site's object store, written, read back and emptied by the API
#   with its own settings -> the egress gate holds an agent pod's first packet -> the agent-isolation
#   probe of S65-04 in its two shapes (a pod, and the Job `k8s_job` renders), under the deployment's
#   RuntimeClass -> one work item carried to `approved` -> the evidence collected -> the release and its
#   namespace removed, whatever the outcome (PROOF_KEEP=1 keeps them) -> the evidence archived.
#
# Needs: kubectl, helm, uv (for the pytest probe), a context whose user may create a namespace.
# Creating and destroying the cluster is not this script's job: tools/cluster-proof/cluster.sh does it for
# kind and k3d (what a managed cluster gives: a CNI that enforces NetworkPolicy, a default IngressClass,
# DNS for the site's names; the `cluster-proof` CI job), a cloud is created by hand until a provider's
# module exists (docs/support/clusters.yaml records each run). On one node of 4 vCPU, give
# tools/cluster-proof/small-node.yaml as an extra file (the profile is sized for 8). The agent of the
# work item is simulated (the demo project's `fake` runtime), as in the single-node proof: the proof is
# about the installation, not a model.
#
#   PROOF_PROFILE    a provider profile under charts/tranibor/values/ (e.g. `aks`), optional
#   PROOF_DOMAIN     the site's domain (default: <ingress IP>.nip.io is the caller's job; required)
#   PROOF_EVIDENCE   where the evidence lands (default: a new temporary directory); it is kept, and
#                    archived beside itself as <PROOF_EVIDENCE>.tar.gz, whatever the outcome
#   PROOF_IMAGE_TAG  `global.imageTag`: the platform's images of this commit, already on the nodes
#                    (tools/cluster-proof/cluster.sh loads them on kind and k3d)
# Exit 0 only if every step held.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
: "${PROOF_CONTEXT:?name the kube context}"
: "${PROOF_DOMAIN:?name the site domain (its auth.<domain> must resolve from the pods)}"
NAMESPACE=${PROOF_NAMESPACE:-tranibor}
RELEASE=tranibor
ORG=proof
ADMIN=admin@proof.example
EVIDENCE=${PROOF_EVIDENCE:-$(mktemp -d)}
STARTED=$(date +%s)
mkdir -p "$EVIDENCE"

step() { printf '\n== [%4ss] %s\n' "$(($(date +%s) - STARTED))" "$*" | tee -a "$EVIDENCE/steps.log"; }
k() { kubectl --context "$PROOF_CONTEXT" -n "$NAMESPACE" "$@"; }
record() { tee -a "$EVIDENCE/steps.log"; }

# Whatever the outcome: what the namespace holds is written down, the release and its namespace are
# removed (unless PROOF_KEEP), and the evidence is kept and archived. Nothing here may stop the rest.
cleanup() {
  local status=$? pod
  set +e
  k get pods -o wide >"$EVIDENCE/pods.txt" 2>&1
  k get events --sort-by=.lastTimestamp >"$EVIDENCE/events.txt" 2>&1
  if [ $status -ne 0 ]; then
    echo "PROOF FAILED (exit $status) — evidence in $EVIDENCE" | record
    # On failure, what each pod said: the step that failed is read from here, the cluster being gone.
    mkdir -p "$EVIDENCE/logs"
    k describe pods >"$EVIDENCE/pods-describe.txt" 2>&1
    for pod in $(k get pods -o name 2>/dev/null); do
      k logs "$pod" --all-containers --prefix --tail=500 >"$EVIDENCE/logs/${pod#pod/}.log" 2>&1
    done
  fi
  if [ -z "${PROOF_KEEP:-}" ]; then
    helm --kube-context "$PROOF_CONTEXT" uninstall "$RELEASE" -n "$NAMESPACE" --wait --timeout 10m >/dev/null 2>&1
    echo "uninstalled $RELEASE (helm exit $?)" | record
    kubectl --context "$PROOF_CONTEXT" delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1
    echo "namespace $NAMESPACE deleted (kubectl exit $?)" | record
  fi
  tar -czf "$EVIDENCE.tar.gz" -C "$(dirname "$EVIDENCE")" "$(basename "$EVIDENCE")" &&
    echo "evidence archived: $EVIDENCE.tar.gz"
  exit $status
}
trap cleanup EXIT

step "the cluster ($PROOF_CONTEXT)"
kubectl --context "$PROOF_CONTEXT" version -o json | tee "$EVIDENCE/version.json" | grep -m1 gitVersion | record
kubectl --context "$PROOF_CONTEXT" get nodes -o wide | record
kubectl --context "$PROOF_CONTEXT" get runtimeclass,storageclass,ingressclass | record

if uv run --project "$ROOT" tranibor cluster check --help >/dev/null 2>&1; then
  step "preflight: what the chart needs, asked of the cluster (S87-03)"
  values=(-f "$ROOT/charts/tranibor/values/all-in-one.yaml")
  [ -n "${PROOF_PROFILE:-}" ] && values+=(-f "$ROOT/charts/tranibor/values/$PROOF_PROFILE.yaml")
  uv run --project "$ROOT" tranibor cluster check --context "$PROOF_CONTEXT" "${values[@]/#-f/--values}" | record
fi

step "install the chart (all-in-one${PROOF_PROFILE:+, profile $PROOF_PROFILE})"
profile=()
[ -n "${PROOF_PROFILE:-}" ] && profile=(-f "$ROOT/charts/tranibor/values/$PROOF_PROFILE.yaml")
extra=()
for file in "$@"; do extra+=(-f "$file"); done
[ -z "${PROOF_IMAGE_TAG:-}" ] || extra+=(--set "global.imageTag=$PROOF_IMAGE_TAG")
helm --kube-context "$PROOF_CONTEXT" install "$RELEASE" "$ROOT/charts/tranibor" -n "$NAMESPACE" --create-namespace \
  -f "$ROOT/charts/tranibor/values/all-in-one.yaml" "${profile[@]}" \
  --set global.domain="$PROOF_DOMAIN" \
  --set global.publicUrl="http://$PROOF_DOMAIN" --set global.apiUrl="http://$PROOF_DOMAIN" \
  --set "tranibor-api.ingress.hostnames={$PROOF_DOMAIN}" --set "tranibor-web.ingress.hostnames={$PROOF_DOMAIN}" \
  --set global.bootstrap.org="$ORG" --set global.bootstrap.orgName=Proof --set global.bootstrap.admins="$ADMIN" \
  "${extra[@]}" --timeout 20m | tee "$EVIDENCE/helm-install.txt" | tail -3 | record
for d in $(k get deploy,statefulset -o name); do k rollout status "$d" --timeout=1200s | record; done

# The single-node proof's probes, unchanged in what they judge (tools/single-node/proof.sh).
PROBE='
import socket, sys, time
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
        sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit(1)
'
probe() {
  local name="probe-$1-$2" image phase
  image=$(k get deploy tranibor-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  k run "$name" --restart=Never --image="$image" --image-pull-policy=IfNotPresent \
    --labels=app.kubernetes.io/name=tranibor-runner,app.kubernetes.io/component=runner,tranibor/sandbox=true,tranibor/run-id=proof \
    --command -- python -c "$PROBE" "$1" "$2" >/dev/null
  for _ in $(seq 90); do
    phase=$(k get pod "$name" -o jsonpath='{.status.phase}')
    case "$phase" in Succeeded | Failed) break ;; esac
    sleep 2
  done
  k delete pod "$name" --wait=false >/dev/null
  case "$phase" in Succeeded) echo open ;; Failed) echo closed ;; *) echo "undecided ($phase)" ;; esac
}

step "an agent pod reaches what a run uses, and not the database, Temporal or the object store"
failed=0
for target in tranibor-api:8000:open tranibor-litellm:4000:open tranibor-ecphoria:8432:open \
  tranibor-egress:3128:open tranibor-postgresql:5432:closed tranibor-temporal:7233:closed \
  tranibor-objectstore:9000:closed; do
  IFS=: read -r host port want <<<"$target"
  got=$(probe "$host" "$port")
  echo "  agent pod -> $host:$port: $got (expected $want)" | record
  [ "$got" = "$want" ] || failed=1
done
[ $failed -eq 0 ]

# The site's object store, as the API uses it (ADR 0049): its own settings and credentials, through the
# platform's own adapter. Checked, written, read back identical, deleted, and then absent.
OBJECT_STORE='
import asyncio, sys
from tranibor_adapters.storage import ObjectNotFoundError
from tranibor_api.config import get_settings
from tranibor_api.object_store import object_store
async def main(key, body):
    store = object_store()
    await store.check()
    where = await store.put(key, body, "text/plain")
    if await store.get(key) != body:
        sys.exit(f"{where}: read back different bytes")
    await store.delete(key)
    try:
        await store.get(key)
    except ObjectNotFoundError:
        pass
    else:
        sys.exit(f"{where}: still there after its deletion")
    settings = get_settings()
    endpoint = settings.object_store_endpoint or "the provider default"
    print(f"{settings.object_store_kind} at {endpoint}: {where} written, read back identical"
          f" ({len(body)} bytes), deleted")
asyncio.run(main(sys.argv[1], sys.argv[2].encode()))
'
step "the site's object store: written, read back and deleted by the API"
k exec deploy/tranibor-api -- python -c "$OBJECT_STORE" "proof/$(date +%s).txt" "written by the install proof" |
  sed 's/^/  /' | record

AGENT_JOB='
import json, os, sys
from types import SimpleNamespace
from tranibor_adapters.executor.k8s_job import KubernetesJobExecutor
from tranibor_core.domain import StageJobSpec
from tranibor_core.egress_gate import gate_from_env
name, image, gated, loop = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
gate = gate_from_env()
if gated and gate is None:
    sys.exit("this deployment does not turn the egress gate on (runner.egressGate.enabled)")
executor = KubernetesJobExecutor(client=SimpleNamespace(), egress_gate=gate if gated else None)
spec = StageJobSpec(run_id=name, project_slug="proof", namespace=os.environ.get("POD_NAMESPACE", "tranibor"),
                    runner_image=image, api_url="http://tranibor-api:8000", run_token="unused",
                    runtime_class=os.environ.get("TRANIBOR_RUNNER_RUNTIME_CLASS") or None)
job = executor._job(spec, name)
pod = job["spec"]["template"]["spec"]
runner = next(c for c in pod["containers"] if c["name"] == "runner")
runner["env"] = [e for e in runner["env"] if "valueFrom" not in e]
runner["command"] = ["/app/.venv/bin/python", "-c", loop]
runner.pop("args", None)
print(json.dumps(job))
'
FIRST_PACKETS='
import json, socket, time
start, tries = time.monotonic(), []
while time.monotonic() - start < 10:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=1).close()
        tries.append("open")
    except OSError:
        tries.append("closed")
    time.sleep(0.2)
print(json.dumps({"first": tries[0], "open": tries.count("open"), "attempts": len(tries)}))
'
agent_pod() {
  local name=$1 gated=$2 image phase
  image=$(k get deploy tranibor-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  k exec deploy/tranibor-orchestrator-executor -- python -c "$AGENT_JOB" "$name" "$image" "$gated" "$FIRST_PACKETS" |
    k apply -f - >/dev/null
  for _ in $(seq 120); do
    phase=$(k get pod -l "job-name=$name" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
    case "$phase" in Succeeded | Failed) break ;; esac
    sleep 2
  done
  echo "$phase"
}

step "an agent pod's first packet out is refused: the egress gate holds it until its policy applies"
phase=$(agent_pod proof-gated 1)
report=$(k logs job/proof-gated -c runner 2>/dev/null || true)
echo "  gated agent pod ($phase), runtimeClass $(k get pod -l job-name=proof-gated -o jsonpath='{.items[0].spec.runtimeClassName}'): gate $(k logs job/proof-gated -c egress-gate 2>/dev/null || true)" | record
echo "  its first direct connections: $report" | record
phase_control=$(agent_pod proof-ungated 0)
echo "  control pod without the gate ($phase_control): $(k logs job/proof-ungated -c runner 2>/dev/null || true)" | record
k delete job proof-gated proof-ungated --wait=false >/dev/null
case "$report" in *'"first": "closed", "open": 0,'*) ;; *) exit 1 ;; esac
[ "$phase" = Succeeded ]

step "the agent's own container: what it reads of the runner's (S65-04), under the deployment's RuntimeClass"
# Read where the executor reads it: its own environment (the chart sets it in a shared block, not always
# as a literal of the Deployment).
RUNTIME_CLASS=$(k exec deploy/tranibor-orchestrator-executor -- sh -c \
  'printf %s "${TRANIBOR_RUNNER_RUNTIME_CLASS:-}"' 2>/dev/null || true)
echo "  RuntimeClass: ${RUNTIME_CLASS:-the cluster default}" | record
TRANIBOR_CLUSTER_CONTEXT="$PROOF_CONTEXT" TRANIBOR_CLUSTER_ALLOW_SHARED="$PROOF_CONTEXT" \
  TRANIBOR_ISOLATION_RUNTIME_CLASS="$RUNTIME_CLASS" \
  uv run --project "$ROOT" pytest -q -s -p no:cacheprovider -m cluster "$ROOT/tests/cluster/test_agent_isolation.py" \
  2>&1 | tee "$EVIDENCE/isolation.txt" | grep -E 'runtimeClass=|passed|failed' | record
grep -qE '[0-9]+ passed' "$EVIDENCE/isolation.txt" && ! grep -qE '[0-9]+ failed' "$EVIDENCE/isolation.txt"
if [ -n "$RUNTIME_CLASS" ]; then grep -q "runtimeClass=$RUNTIME_CLASS" "$EVIDENCE/isolation.txt"; fi

step "one work item to completion"
TOKEN=$(k exec deploy/tranibor-api -- tranibor-admin tokens create --email "$ADMIN" --name proof --expires-in-days 1)
ITEM=$(k exec -i deploy/tranibor-api -- python - run "$ORG" "$TOKEN" <"$ROOT/tools/single-node/work_item.py")
echo "  $ITEM" | record
k exec -i deploy/tranibor-api -- python - check "$ORG" "$TOKEN" "$ITEM" <"$ROOT/tools/single-node/work_item.py" | record

step "PROOF HELD — evidence in $EVIDENCE"
