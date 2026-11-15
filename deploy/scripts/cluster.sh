#!/usr/bin/env bash
# Creates the DigitalOcean Kubernetes cluster and the container registry for Burn Rate.
#
#   deploy/scripts/cluster.sh            # show what would be created and what it costs; creates NOTHING
#   deploy/scripts/cluster.sh --yes      # create (idempotent: what already exists is left alone)
#   deploy/scripts/cluster.sh --help
#
# Creates, with --yes only:
#   - DOKS cluster $CLUSTER in $REGION (default nyc1), newest Kubernetes $K8S_MINOR.x, one pool "main":
#     $NODE_COUNT x $NODE_SIZE (default 2 x s-4vcpu-8gb). Control plane not highly available: free.
#     Surge upgrades are OFF: the account allows 3 droplets, the edge server burnrate-app uses one, the two
#     nodes use the rest, so there is no droplet left for the extra node a surge upgrade needs.
#   - container registry $REGISTRY_NAME, plan "basic" (repositories api and web appear on first push)
#   - a kubectl context for the cluster, made current
# It first checks that the account's droplet limit leaves room for the nodes, and stops if it does not.
# Does NOT create the load balancer and the volumes: those appear when bootstrap.sh installs the gateway and
# the data stores, and are listed below because they are part of the bill.
#
# Token: $DIGITALOCEAN_ACCESS_TOKEN, else the file ~/.do_token.
# Environment: CLUSTER, REGION, REGISTRY_NAME, REGISTRY_REGION (default: DigitalOcean's choice),
#              K8S_VERSION (exact slug, e.g. 1.35.10-do.0), NODE_SIZE, NODE_COUNT.
# The opposite of this script is deploy/scripts/teardown.sh.
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
check_profile

# ---- prices, US dollars a month (checked on digitalocean.com on 2026-11-15; see deploy/README.md "Cost") ----
PRICE_NODE=48            # s-4vcpu-8gb, Basic Regular: $48.00 a month, $0.07143 an hour
PRICE_LB=12              # regional load balancer, per node (size unit 1)
PRICE_REGISTRY=5         # Container Registry, plan basic: ONE registry, 5 repositories, 5 GiB
PRICE_VOLUME_CENTS=10    # block storage, per GiB a month
PRICE_EDGE=24            # the edge droplet burnrate-app (s-2vcpu-4gb): exists already, NOT created or deleted here
if [[ "$PROFILE" == "small" ]]; then
  VOLUMES_GIB=36         # PostgreSQL 2 x 10, Kafka 10, Prometheus 5, Valkey 1
else
  VOLUMES_GIB=176        # PostgreSQL 3 x 20, Kafka 3 x 20, Prometheus 20, Loki 10, Tempo 10, Vault 3 x 5, Valkey 1
fi
[[ "$NODE_SIZE" == "s-4vcpu-8gb" ]] || PRICE_NODE=0

nodes_cost=$(( NODE_COUNT * PRICE_NODE ))
vol_cents=$(( VOLUMES_GIB * PRICE_VOLUME_CENTS ))
total_cents=$(( (nodes_cost + PRICE_LB + PRICE_REGISTRY) * 100 + vol_cents ))
money() { printf '$%d.%02d' $(( $1 / 100 )) $(( $1 % 100 )); }

log "Burn Rate on DigitalOcean (profile $PROFILE)"
info "cluster   $CLUSTER, region $REGION, Kubernetes ${K8S_VERSION:-newest $K8S_MINOR.x}, control plane not HA (free)"
info "nodes     $NODE_COUNT x $NODE_SIZE, pool \"main\""
info "registry  registry.digitalocean.com/$REGISTRY_NAME (plan basic)"
echo
info "cost per month"
if (( PRICE_NODE > 0 )); then
  info "  nodes            $NODE_COUNT x \$$PRICE_NODE              $(money $(( nodes_cost * 100 )))   created by this script"
else
  info "  nodes            $NODE_COUNT x $NODE_SIZE     price not known to this script (doctl compute size list)"
fi
info "  registry         basic                 $(money $(( PRICE_REGISTRY * 100 )))   created by this script"
info "  load balancer    1 x \$$PRICE_LB              $(money $(( PRICE_LB * 100 )))   created later by bootstrap.sh (gateway)"
info "  volumes          $VOLUMES_GIB GiB x \$0.10       $(money "$vol_cents")   created later by bootstrap.sh (data stores)"
if (( PRICE_NODE > 0 )); then
  info "  total                                  $(money "$total_cents") a month, about $(money $(( total_cents / 30 ))) a day"
fi
info "Nodes are billed by the second (about \$0.071 an hour each); everything above stops costing when teardown.sh has run."
info "Not in the total: the edge droplet burnrate-app that already exists (s-2vcpu-4gb, about \$$PRICE_EDGE a month)."
if [[ "$PROFILE" == "full" ]]; then
  warn "profile full does not fit $NODE_COUNT nodes of this size: it asks for about 27 GiB of memory (deploy/README.md)"
fi

if [[ $YES -ne 1 ]]; then
  log "nothing was created. Re-run with --yes to create the cluster and the registry."
  exit 0
fi

# ---- from here on things are created -----------------------------------------------------------------------
need doctl jq kubectl
load_do_token
doctl account get >/dev/null 2>&1 || die "doctl cannot reach the account with this token"
mkdir -p "$STATE_DIR"

log "cluster $CLUSTER"
if doctl kubernetes cluster get "$CLUSTER" >/dev/null 2>&1; then
  info "already exists, left as it is"
  doctl kubernetes cluster kubeconfig save "$CLUSTER" >/dev/null
else
  # Every node is a droplet. Asking for more than the account may have fails half-way, so look first.
  limit=$(doctl account get -o json 2>/dev/null | jq -r '(if type == "array" then .[0] else . end) | .droplet_limit // empty' 2>/dev/null || true)
  used=$(doctl compute droplet list -o json 2>/dev/null | jq 'length' 2>/dev/null || true)
  if [[ "$limit" =~ ^[0-9]+$ && "$used" =~ ^[0-9]+$ ]]; then
    info "droplets: $used in use, limit $limit, this cluster needs $NODE_COUNT"
    (( used + NODE_COUNT <= limit )) || die "not enough room: $used droplets exist and the limit is $limit, so at most $(( limit - used )) node(s) fit (NODE_COUNT=$NODE_COUNT). Nothing was created."
  else
    warn "could not read the droplet limit of the account; continuing"
  fi
  version=${K8S_VERSION:-}
  if [[ -z "$version" ]]; then
    version=$(doctl kubernetes options versions -o json \
      | jq -r --arg p "$K8S_MINOR." '[.[] | select(.slug | startswith($p))][0].slug // empty')
    [[ -n "$version" ]] || die "DigitalOcean offers no Kubernetes $K8S_MINOR.x (doctl kubernetes options versions); set K8S_VERSION"
  fi
  info "creating: $NODE_COUNT x $NODE_SIZE, Kubernetes $version (takes 5-8 minutes)"
  doctl kubernetes cluster create "$CLUSTER" \
    --region "$REGION" \
    --version "$version" \
    --node-pool "name=main;size=$NODE_SIZE;count=$NODE_COUNT" \
    --ha=false \
    --auto-upgrade=false \
    --surge-upgrade=false \
    --tag burnrate \
    --wait \
    --update-kubeconfig=true \
    --set-current-context=true >/dev/null
  info "created"
fi
id=$(doctl kubernetes cluster get "$CLUSTER" -o json | jq -r '(if type == "array" then .[0] else . end) | .id // empty')
[[ -n "$id" ]] && state_put cluster-id "$id"
info "kubectl context: $(kubectl config current-context)"
kubectl get nodes -o wide 2>/dev/null | sed 's/^/    /' || warn "kubectl cannot list the nodes yet"

log "registry $REGISTRY_NAME"
existing=$(doctl registry get -o json 2>/dev/null | jq -r '(if type == "array" then .[0] else . end) | .name // empty' 2>/dev/null || true)
if [[ "$existing" == "$REGISTRY_NAME" ]]; then
  info "already exists, left as it is"
elif [[ -n "$existing" ]]; then
  # The basic plan allows ONE registry per account. Deleting somebody's images is not this script's call.
  die "this account already has the registry '$existing', and the basic plan allows only one.
       Either keep it and use it:   export REGISTRY_NAME=$existing   (for every script in deploy/scripts/)
       or remove it yourself:       doctl registry delete $existing   and run this script again"
else
  region_args=()
  [[ -n "${REGISTRY_REGION:-}" ]] && region_args=(--region "$REGISTRY_REGION")
  doctl registry create "$REGISTRY_NAME" --subscription-tier basic ${region_args[@]+"${region_args[@]}"} >/dev/null \
    || die "could not create the registry '$REGISTRY_NAME' (registry names are unique across DigitalOcean: try REGISTRY_NAME=burnrate-<something>)"
  info "created registry.digitalocean.com/$REGISTRY_NAME"
fi

log "done. Next: PROFILE=$PROFILE deploy/scripts/bootstrap.sh"
