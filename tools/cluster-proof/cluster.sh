#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# A throwaway local cluster for the install proof (S87-09): kind or k3d, made into what a managed
# cluster gives the chart, and nothing more.
#
#   tools/cluster-proof/cluster.sh up   kind|k3d <name> <domain>   # prints the kube context
#   tools/cluster-proof/cluster.sh down kind|k3d <name>
#
# What `up` adds to the bare cluster, each pinned and checked against its checksum:
#   - a CNI that enforces NetworkPolicy: Calico v3.28.2 on kind (kindnet accepts a policy and lets the
#     traffic through, S87-03); k3s's own kube-router on k3d, left on;
#   - a default IngressClass: ingress-nginx (kind provider manifest) on kind; k3s's Traefik on k3d;
#   - DNS: `<domain>` and every name under it (`auth.<domain>`, the embedded Keycloak) resolve, FROM THE
#     PODS, to the ingress controller's Service, through a CoreDNS server block for the zone. The API reads
#     the issuer's discovery document at `http://auth.<domain>`: a site's DNS does that on a real cluster.
#   - the images named by PROOF_LOAD_IMAGES ("img:tag ..."), loaded into the nodes: the chart of this
#     commit with its own images (`global.imageTag`), as the single-node proof imports them.
#
#   PROOF_NODE_IMAGE  the node image (kind's or k3d's default otherwise): CI pins it by digest.
#   PROOF_KIND_CONFIG a kind configuration in place of tools/cluster-proof/kind.yaml (a machine whose
#                     sandbox needs a containerd setting, for instance).
#   PROOF_EVICTION    k3d: the kubelet's hard eviction thresholds (imagefs and nodefs), for a disk more
#                     than 85 % used once the images are in (kind turns disk eviction off itself).
#   PROOF_NODE_CA     for a machine behind a TLS-inspecting proxy (its HTTPS_PROXY and NO_PROXY reach the
#                     nodes): a PEM bundle the nodes' container runtime trusts, the machine's whole trust
#                     store with the proxy's authority in it. kind adds it to the node's store; k3d mounts
#                     it as the node's store. Nothing of it reaches a pod.
#   PROOF_K3D_ARGS    k3d: more `k3d cluster create` arguments, split on spaces (a registry mirror's
#                     `--registry-config`, for instance).
# Needs: docker, kubectl, curl, sha256sum, and kind or k3d.
set -euo pipefail

CALICO_URL=https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
CALICO_SHA256=be59408bf990e96276f631d2f9285c2a0f9802194c0ad1cecdb6d9c52623a1c8
INGRESS_NGINX_URL=https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.13.3/deploy/static/provider/kind/deploy.yaml
INGRESS_NGINX_SHA256=6365566a906aede0977b1dfaf355a01398cd076b377864e3114d3eab52442346

usage() {
  echo "usage: $0 up kind|k3d <name> <domain> | down kind|k3d <name>" >&2
  exit 2
}
[ $# -ge 3 ] || usage
ACTION=$1 FLAVOUR=$2 NAME=$3
case "$FLAVOUR" in kind) CONTEXT=kind-$NAME ;; k3d) CONTEXT=k3d-$NAME ;; *) usage ;; esac
kc() { kubectl --context "$CONTEXT" "$@"; }

# A manifest, fetched and refused unless its checksum is the pinned one.
manifest() {
  local url=$1 sum=$2 file
  file=$(mktemp)
  curl -sSfL --retry 5 -o "$file" "$url"
  echo "$sum  $file" | sha256sum -c - >/dev/null || {
    echo "checksum mismatch for $url" >&2
    exit 1
  }
  echo "$file"
}

# kind: the node's containerd is restarted once its store holds the bundle, before it pulls anything.
trust_ca() {
  [ -n "${PROOF_NODE_CA:-}" ] || return 0
  local node
  for node in $(kind get nodes --name "$NAME"); do
    docker cp "$PROOF_NODE_CA" "$node:/usr/local/share/ca-certificates/proof-node-ca.crt"
    docker exec "$node" sh -c 'update-ca-certificates >/dev/null 2>&1 && systemctl restart containerd'
  done
}

# CoreDNS answers `<domain>` and every name under it with the ingress controller's ClusterIP.
dns() {
  local domain=$1 ip=$2 block
  block="$domain:53 {
    errors
    template IN A $domain {
        answer \"{{ .Name }} 60 IN A $ip\"
    }
    template IN AAAA $domain {
        rcode NOERROR
    }
}"
  if [ "$FLAVOUR" = kind ]; then
    local corefile
    corefile=$(kc -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}')
    printf '%s\n%s\n' "$corefile" "$block" >"$WORK/Corefile"
    kc -n kube-system create configmap coredns --from-file=Corefile="$WORK/Corefile" \
      --dry-run=client -o yaml | kc apply -f - >/dev/null
  else
    # k3s imports `/etc/coredns/custom/*.server` from this ConfigMap.
    printf '%s\n' "$block" >"$WORK/proof.server"
    kc -n kube-system create configmap coredns-custom --from-file=proof.server="$WORK/proof.server" \
      --dry-run=client -o yaml | kc apply -f - >/dev/null
  fi
  # The rollout is done before the old pods are gone: until they are, the Service still sends some
  # queries to a server without the block (measured on kind: the first lookup from a pod failed).
  local old
  old=$(kc -n kube-system get pods -l k8s-app=kube-dns -o name)
  kc -n kube-system rollout restart deploy/coredns >/dev/null
  kc -n kube-system rollout status deploy/coredns --timeout=180s
  # shellcheck disable=SC2086 # a list of pod names
  kc -n kube-system wait --for=delete $old --timeout=180s
}

up() {
  local domain=$1 ip
  WORK=$(mktemp -d)
  if [ "$FLAVOUR" = kind ]; then
    local image=()
    [ -z "${PROOF_NODE_IMAGE:-}" ] || image=(--image "$PROOF_NODE_IMAGE")
    kind create cluster --name "$NAME" --config "${PROOF_KIND_CONFIG:-$(dirname "$0")/kind.yaml}" "${image[@]}" --wait 0s >&2
    trust_ca
    kc apply -f "$(manifest "$CALICO_URL" "$CALICO_SHA256")" >/dev/null
    kc -n kube-system rollout status ds/calico-node --timeout=600s >&2
    kc wait --for=condition=Ready nodes --all --timeout=300s >&2
    # shellcheck disable=SC2086 # a list of image references
    [ -z "${PROOF_LOAD_IMAGES:-}" ] || kind load docker-image --name "$NAME" $PROOF_LOAD_IMAGES >&2
    kc apply -f "$(manifest "$INGRESS_NGINX_URL" "$INGRESS_NGINX_SHA256")" >/dev/null
    kc -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=600s >&2
    # The chart's ingresses name no class (values/all-in-one.yaml): the cluster's default takes them.
    kc annotate ingressclass nginx ingressclass.kubernetes.io/is-default-class=true --overwrite >/dev/null
    ip=$(kc -n ingress-nginx get service ingress-nginx-controller -o jsonpath='{.spec.clusterIP}')
  else
    local image=()
    [ -z "${PROOF_NODE_IMAGE:-}" ] || image=(--image "$PROOF_NODE_IMAGE")
    local extra=()
    for variable in HTTPS_PROXY NO_PROXY; do
      [ -z "${!variable:-}" ] || extra+=(-e "$variable=${!variable}@server:*")
    done
    # k3s pulls its own images as it starts: the bundle must be there before, not added after.
    [ -z "${PROOF_NODE_CA:-}" ] || extra+=(--volume "$PROOF_NODE_CA:/etc/ssl/certs/ca-certificates.crt@server:*")
    # shellcheck disable=SC2206 # split on spaces, by design
    [ -z "${PROOF_K3D_ARGS:-}" ] || extra+=($PROOF_K3D_ARGS)
    if [ -n "${PROOF_EVICTION:-}" ]; then
      local t="memory.available<100Mi,imagefs.available<$PROOF_EVICTION,nodefs.available<$PROOF_EVICTION"
      extra+=(--k3s-arg "--kubelet-arg=eviction-hard=$t@server:*")
    fi
    k3d cluster create "$NAME" --wait --timeout 300s --kubeconfig-update-default --kubeconfig-switch-context=false \
      "${image[@]}" "${extra[@]}" >&2
    kc wait --for=condition=Ready nodes --all --timeout=300s >&2
    # shellcheck disable=SC2086 # a list of image references
    [ -z "${PROOF_LOAD_IMAGES:-}" ] || k3d image import -c "$NAME" $PROOF_LOAD_IMAGES >&2
    for _ in $(seq 90); do
      ip=$(kc -n kube-system get service traefik -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
      [ -n "$ip" ] && break
      sleep 2
    done
    [ -n "$ip" ] || {
      echo "k3s's Traefik never came up" >&2
      exit 1
    }
    kc -n kube-system rollout status deploy/traefik --timeout=600s >&2
  fi
  dns "$domain" "$ip" >&2
  echo "$CONTEXT"
}

down() {
  case "$FLAVOUR" in
    kind) kind delete cluster --name "$NAME" >&2 ;;
    k3d) k3d cluster delete "$NAME" >&2 ;;
  esac
}

case "$ACTION" in
  up)
    [ $# -eq 4 ] || usage
    up "$4"
    ;;
  down) down ;;
  *) usage ;;
esac
