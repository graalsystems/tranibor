#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The install proof on ANY cluster (S87-09, ADR 0226 §3): the single-node proof's checks, made portable.
#
#   PROOF_CONTEXT=<kube context> tools/cluster-proof/proof.sh [extra values files...]
#
#   the cluster preflight (when the CLI has it) -> the chart with values/all-in-one.yaml, then the
#   provider profile and the files given -> an agent pod reaches what a run uses and not the database
#   or Temporal -> the egress gate holds an agent pod's first packet -> the agent-isolation probe of
#   S65-04 in its two shapes (a pod, and the Job `k8s_job` renders), under the deployment's RuntimeClass
#   -> one work item carried to `approved` -> the evidence collected -> the release and its namespace
#   removed, whatever the outcome (PROOF_KEEP=1 keeps them).
#
# Needs: kubectl, helm, uv (for the pytest probe), a context whose user may create a namespace.
# Creating and destroying the cluster is not this script's job: a provider's module does that
# (docs/support/clusters.yaml records each run). The agent of the work item is simulated (the demo
# project's `fake` runtime), as in the single-node proof: the proof is about the installation, not a
# model.
#
#   PROOF_PROFILE    a provider profile under charts/choregos/values/ (e.g. `aks`), optional
#   PROOF_DOMAIN     the site's domain (default: <ingress IP>.nip.io is the caller's job; required)
#   PROOF_EVIDENCE   where the evidence lands (default: a new temporary directory)
# Exit 0 only if every step held.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
: "${PROOF_CONTEXT:?name the kube context}"
: "${PROOF_DOMAIN:?name the site domain (its auth.<domain> must resolve from the pods)}"
NAMESPACE=${PROOF_NAMESPACE:-choregos}
RELEASE=choregos
ORG=proof
ADMIN=admin@proof.example
EVIDENCE=${PROOF_EVIDENCE:-$(mktemp -d)}
STARTED=$(date +%s)
mkdir -p "$EVIDENCE"

step() { printf '\n== [%4ss] %s\n' "$(($(date +%s) - STARTED))" "$*" | tee -a "$EVIDENCE/steps.log"; }
k() { kubectl --context "$PROOF_CONTEXT" -n "$NAMESPACE" "$@"; }
record() { tee -a "$EVIDENCE/steps.log"; }

cleanup() {
  local status=$?
  k get pods -o wide >"$EVIDENCE/pods.txt" 2>&1 || true
  k get events --sort-by=.lastTimestamp >"$EVIDENCE/events.txt" 2>&1 || true
  if [ $status -ne 0 ]; then echo "PROOF FAILED (exit $status) — evidence in $EVIDENCE" | record; fi
  if [ -z "${PROOF_KEEP:-}" ]; then
    helm --kube-context "$PROOF_CONTEXT" uninstall "$RELEASE" -n "$NAMESPACE" --wait --timeout 10m >/dev/null 2>&1 || true
    kubectl --context "$PROOF_CONTEXT" delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

step "the cluster ($PROOF_CONTEXT)"
kubectl --context "$PROOF_CONTEXT" version -o json | tee "$EVIDENCE/version.json" | grep -m1 gitVersion | record
kubectl --context "$PROOF_CONTEXT" get nodes -o wide | record
kubectl --context "$PROOF_CONTEXT" get runtimeclass,storageclass,ingressclass | record

if uv run --project "$ROOT" tranibor cluster check --help >/dev/null 2>&1; then
  step "preflight: what the chart needs, asked of the cluster (S87-03)"
  values=(-f "$ROOT/charts/choregos/values/all-in-one.yaml")
  [ -n "${PROOF_PROFILE:-}" ] && values+=(-f "$ROOT/charts/choregos/values/$PROOF_PROFILE.yaml")
  uv run --project "$ROOT" tranibor cluster check --context "$PROOF_CONTEXT" "${values[@]/#-f/--values}" | record
fi

step "install the chart (all-in-one${PROOF_PROFILE:+, profile $PROOF_PROFILE})"
profile=()
[ -n "${PROOF_PROFILE:-}" ] && profile=(-f "$ROOT/charts/choregos/values/$PROOF_PROFILE.yaml")
extra=()
for file in "$@"; do extra+=(-f "$file"); done
helm --kube-context "$PROOF_CONTEXT" install "$RELEASE" "$ROOT/charts/choregos" -n "$NAMESPACE" --create-namespace \
  -f "$ROOT/charts/choregos/values/all-in-one.yaml" "${profile[@]}" \
  --set global.domain="$PROOF_DOMAIN" \
  --set global.publicUrl="http://$PROOF_DOMAIN" --set global.apiUrl="http://$PROOF_DOMAIN" \
  --set "choregos-api.ingress.hostnames={$PROOF_DOMAIN}" --set "choregos-web.ingress.hostnames={$PROOF_DOMAIN}" \
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
  image=$(k get deploy choregos-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  k run "$name" --restart=Never --image="$image" --image-pull-policy=IfNotPresent \
    --labels=app.kubernetes.io/name=choregos-runner,app.kubernetes.io/component=runner,choregos/sandbox=true,choregos/run-id=proof \
    --command -- python -c "$PROBE" "$1" "$2" >/dev/null
  for _ in $(seq 90); do
    phase=$(k get pod "$name" -o jsonpath='{.status.phase}')
    case "$phase" in Succeeded | Failed) break ;; esac
    sleep 2
  done
  k delete pod "$name" --wait=false >/dev/null
  case "$phase" in Succeeded) echo open ;; Failed) echo closed ;; *) echo "undecided ($phase)" ;; esac
}

step "an agent pod reaches what a run uses, and not the database or Temporal"
failed=0
for target in choregos-api:8000:open choregos-litellm:4000:open choregos-ecphoria:8432:open \
  choregos-egress:3128:open choregos-postgresql:5432:closed choregos-temporal:7233:closed; do
  IFS=: read -r host port want <<<"$target"
  got=$(probe "$host" "$port")
  echo "  agent pod -> $host:$port: $got (expected $want)" | record
  [ "$got" = "$want" ] || failed=1
done
[ $failed -eq 0 ]

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
spec = StageJobSpec(run_id=name, project_slug="proof", namespace=os.environ.get("POD_NAMESPACE", "choregos"),
                    runner_image=image, api_url="http://choregos-api:8000", run_token="unused",
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
  image=$(k get deploy choregos-api -o jsonpath='{.spec.template.spec.containers[0].image}')
  k exec deploy/choregos-orchestrator-executor -- python -c "$AGENT_JOB" "$name" "$image" "$gated" "$FIRST_PACKETS" |
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
RUNTIME_CLASS=$(k exec deploy/choregos-orchestrator-executor -- sh -c \
  'printf %s "${TRANIBOR_RUNNER_RUNTIME_CLASS:-${CHOREGOS_RUNNER_RUNTIME_CLASS:-}}"' 2>/dev/null || true)
echo "  RuntimeClass: ${RUNTIME_CLASS:-the cluster default}" | record
TRANIBOR_CLUSTER_CONTEXT="$PROOF_CONTEXT" TRANIBOR_CLUSTER_ALLOW_SHARED="$PROOF_CONTEXT" \
  TRANIBOR_ISOLATION_RUNTIME_CLASS="$RUNTIME_CLASS" \
  uv run --project "$ROOT" pytest -q -s -p no:cacheprovider -m cluster "$ROOT/tests/cluster/test_agent_isolation.py" \
  2>&1 | tee "$EVIDENCE/isolation.txt" | grep -E 'runtimeClass=|passed|failed' | record
grep -qE '[0-9]+ passed' "$EVIDENCE/isolation.txt" && ! grep -qE '[0-9]+ failed' "$EVIDENCE/isolation.txt"
if [ -n "$RUNTIME_CLASS" ]; then grep -q "runtimeClass=$RUNTIME_CLASS" "$EVIDENCE/isolation.txt"; fi

step "one work item to completion"
TOKEN=$(k exec deploy/choregos-api -- choregos-admin tokens create --email "$ADMIN" --name proof --expires-in-days 1)
ITEM=$(k exec -i deploy/choregos-api -- python - run "$ORG" "$TOKEN" <"$ROOT/tools/single-node/work_item.py")
echo "  $ITEM" | record
k exec -i deploy/choregos-api -- python - check "$ORG" "$TOKEN" "$ITEM" <"$ROOT/tools/single-node/work_item.py" | record

step "PROOF HELD — evidence in $EVIDENCE"
