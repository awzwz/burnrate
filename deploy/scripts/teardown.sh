#!/usr/bin/env bash
# Deletes the DOKS cluster and the DigitalOcean resources it created:
#   - the cluster itself (all nodes)
#   - the block-storage volumes of its PersistentVolumeClaims (pvc-*, tagged k8s:<cluster-id>)
#   - the load balancer of the gateway (name burnrate-lb, or tagged k8s:<cluster-id>)
# ALL DATA IN THE CLUSTER IS LOST: PostgreSQL, Kafka, metrics.
#
#   deploy/scripts/teardown.sh           # DRY RUN: only lists what would be deleted
#   deploy/scripts/teardown.sh --yes     # delete
#   deploy/scripts/teardown.sh --help
#
# Never touched: the container registry (it costs $5 a month on its own; remove it with
# `doctl registry delete <name>` if you want), anything not linked to this cluster, and the local files in
# deploy/secrets/ (delete that directory yourself before creating a new cluster, so new passwords are generated).
# Token: $DIGITALOCEAN_ACCESS_TOKEN, else ~/.do_token. Environment: CLUSTER, LB_NAME.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

YES=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --dry-run) YES=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $arg" ;;
  esac
done
need doctl jq
load_do_token

ID=$(doctl kubernetes cluster get "$CLUSTER" -o json 2>/dev/null | jq -r '(if type == "array" then .[0] else . end) | .id // empty' 2>/dev/null || true)
if [[ -n "$ID" ]]; then
  state_put cluster-id "$ID"
elif [[ -n "$(state_get cluster-id)" ]]; then
  ID=$(state_get cluster-id)
  warn "cluster $CLUSTER not found; using the saved id $ID to look for leftovers"
else
  die "cluster $CLUSTER not found and no saved id in deploy/.state/cluster-id: nothing to do"
fi
TAG="k8s:$ID"

leftover_volumes() {
  doctl compute volume list -o json \
    | jq -r --arg t "$TAG" '.[] | select((.tags // []) | index($t)) | select(.name | startswith("pvc-")) | "\(.id) \(.name) \(.size_gigabytes)GiB attached=\(.droplet_ids|length)"'
}
leftover_lbs() {
  doctl compute load-balancer list -o json \
    | jq -r --arg t "$TAG" --arg n "$LB_NAME" '.[] | select(.name == $n or ((.tags // []) | index($t))) | "\(.id) \(.name) \(.ip)"'
}

log "cluster"
doctl kubernetes cluster get "$CLUSTER" --format ID,Name,Region,Status,NodePools 2>/dev/null || info "(already deleted)"
log "volumes tagged $TAG (PersistentVolumeClaims)"
leftover_volumes | sed 's/^/    /' || true
log "load balancers ($LB_NAME or tagged $TAG)"
leftover_lbs | sed 's/^/    /' || true
info "the registry registry.digitalocean.com/$REGISTRY_NAME is NOT deleted"

if [[ $YES -ne 1 ]]; then
  log "dry run only: nothing was deleted. Re-run with --yes to delete all of the above."
  exit 0
fi

if doctl kubernetes cluster get "$CLUSTER" >/dev/null 2>&1; then
  log "deleting cluster $CLUSTER with its volumes and load balancers (--dangerous)"
  doctl kubernetes cluster delete "$CLUSTER" --dangerous --force
fi

log "sweeping leftovers (volumes detach a minute or two after the nodes are gone)"
for _ in $(seq 1 20); do
  vols=$(leftover_volumes || true); lbs=$(leftover_lbs || true)
  [[ -z "$vols" && -z "$lbs" ]] && break
  while read -r id name size att; do
    [[ -z "$id" ]] && continue
    if [[ "$att" == "attached=0" ]]; then
      doctl compute volume delete "$id" --force && info "deleted volume $name ($size)"
    else
      info "volume $name is still attached, waiting"
    fi
  done <<< "$vols"
  while read -r id name ip; do
    [[ -z "$id" ]] && continue
    doctl compute load-balancer delete "$id" --force && info "deleted load balancer $name ($ip)"
  done <<< "$lbs"
  sleep 20
done

vols=$(leftover_volumes || true); lbs=$(leftover_lbs || true)
if [[ -n "$vols$lbs" ]]; then
  warn "still present, run again in a few minutes: deploy/scripts/teardown.sh --yes"
  printf '%s\n%s\n' "$vols" "$lbs"
  exit 1
fi
rm -f "$STATE_DIR/lb-ip" "$STATE_DIR/host" "$STATE_DIR/image-tag"
log "teardown complete (registry kept; kubectl context of the deleted cluster: kubectl config delete-context <name>)"
