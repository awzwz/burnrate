# shellcheck shell=bash
# Shared helpers for the deploy scripts. Source it, do not run it.
# Written for bash 3.2 (the bash of macOS): no associative arrays, no mapfile.
# Nothing here prints a secret value: secrets go file -> kubectl, output is discarded.

DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
REPO_DIR=$(cd "$DEPLOY_DIR/.." && pwd)
PLATFORM_DIR=$DEPLOY_DIR/platform
APPS_DIR=$DEPLOY_DIR/apps
SECRETS_DIR=$DEPLOY_DIR/secrets     # generated passwords and keys (gitignored, chmod 700)
STATE_DIR=$DEPLOY_DIR/.state        # nothing secret: load balancer IP, last host, deploy history (gitignored)

# shellcheck source=../versions.env
source "$DEPLOY_DIR/versions.env"

PROFILE=${PROFILE:-small}                                  # small | full
CLUSTER=${CLUSTER:-burnrate}                               # DOKS cluster name
REGION=${REGION:-nyc3}
REGISTRY_NAME=${REGISTRY_NAME:-burnrate}                   # DigitalOcean Container Registry name (globally unique)
REGISTRY=${REGISTRY:-registry.digitalocean.com/$REGISTRY_NAME}
APP_NS=burnrate
LB_NAME=${LB_NAME:-burnrate-lb}                            # also in deploy/platform/envoy-gateway/proxy-*.yaml
DRY_RUN=${DRY_RUN:-0}

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail] %s\033[0m\n' "$*" >&2; exit 1; }
dry()  { [[ "$DRY_RUN" == "1" ]]; }

need() { # need <command>...
  local c
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "$c is required but not installed"; done
}

check_profile() {
  case "$PROFILE" in
    small|full) ;;
    *) die "PROFILE must be small or full (got: $PROFILE)" ;;
  esac
}

# The DigitalOcean token: $DIGITALOCEAN_ACCESS_TOKEN, else ~/.do_token. doctl reads the variable itself.
load_do_token() {
  if [[ -z "${DIGITALOCEAN_ACCESS_TOKEN:-}" && -f "$HOME/.do_token" ]]; then
    DIGITALOCEAN_ACCESS_TOKEN=$(tr -d '[:space:]' < "$HOME/.do_token")
    export DIGITALOCEAN_ACCESS_TOKEN
  fi
  [[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]] || die "no DigitalOcean token: set DIGITALOCEAN_ACCESS_TOKEN or put it in ~/.do_token"
}

# Refuse to touch a cluster that is not ours (kubectl may point at anything).
require_context() {
  dry && return 0
  need kubectl
  local ctx
  ctx=$(kubectl config current-context 2>/dev/null || true)
  [[ -n "$ctx" ]] || die "kubectl has no current context (doctl kubernetes cluster kubeconfig save $CLUSTER)"
  if [[ "$ctx" != *"$CLUSTER"* && "${ALLOW_ANY_CONTEXT:-0}" != "1" ]]; then
    die "kubectl context is '$ctx', not the $CLUSTER cluster (ALLOW_ANY_CONTEXT=1 to override)"
  fi
}

# helm_up <release> <namespace> <chart> <version> <values-file> [extra helm args...]
# Dry run: renders the chart with the same values (helm template) and installs nothing.
helm_up() {
  local rel=$1 ns=$2 chart=$3 ver=$4 values=$5; shift 5
  if dry; then
    helm template "$rel" "$chart" -n "$ns" --version "$ver" -f "$values" "$@" >/dev/null \
      || die "helm template failed: $chart $ver with $values"
    info "[dry-run] helm upgrade --install $rel $chart --version $ver -n $ns   (rendered without errors)"
    return 0
  fi
  helm upgrade --install "$rel" "$chart" -n "$ns" --create-namespace \
    --version "$ver" -f "$values" --wait --timeout "${HELM_TIMEOUT:-15m}" "$@" >/dev/null
  info "helm release $ns/$rel ($chart $ver) deployed"
}

# kubectl wrappers that only print in a dry run
kapply() {
  if dry; then info "[dry-run] kubectl apply $*"; return 0; fi
  kubectl apply "$@" >/dev/null
}
kwait() {
  if dry; then info "[dry-run] kubectl wait $*"; return 0; fi
  kubectl wait "$@" >/dev/null
}

rand() { openssl rand -hex "${1:-20}"; }

# ensure_env_file <file> KEY=value|@rand|@hex64 ...  -> creates the file once (chmod 600), never overwrites.
ensure_env_file() {
  local f=$1; shift
  if dry; then
    [[ -f "$f" ]] || info "[dry-run] would generate ${f#"$DEPLOY_DIR"/}"
    return 0
  fi
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  [[ -f "$f" ]] && return 0
  local kv k v old_umask
  old_umask=$(umask); umask 077
  : > "$f"
  for kv in "$@"; do
    k=${kv%%=*}; v=${kv#*=}
    [[ "$v" == "@rand" ]] && v=$(rand 20)
    [[ "$v" == "@hex64" ]] && v=$(rand 32)
    printf '%s=%s\n' "$k" "$v" >> "$f"
  done
  chmod 600 "$f"
  umask "$old_umask"
  info "generated ${f#"$DEPLOY_DIR"/} (gitignored)"
}

# env_get <file> <KEY>  -> value (for piping into other files only; never echo it to the terminal)
env_get() { grep -E "^$2=" "$1" | head -1 | cut -d= -f2-; }

# secret_from_env_file <namespace> <name> <file>
secret_from_env_file() {
  if dry; then info "[dry-run] secret $1/$2 from ${3#"$DEPLOY_DIR"/}"; return 0; fi
  kubectl -n "$1" create secret generic "$2" --from-env-file="$3" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  info "secret $1/$2 applied"
}

# The password of the PostgreSQL role "burnrate": generated once into deploy/secrets/pg.env and given to
# CloudNativePG as the basic-auth Secret data/pg-burnrate (the label makes the operator re-read it on change).
PG_ENV=$SECRETS_DIR/pg.env
apply_pg_secret() {
  ensure_env_file "$PG_ENV" PG_USER=burnrate PG_PASSWORD=@hex64
  if dry; then info "[dry-run] secret data/pg-burnrate (basic-auth) from deploy/secrets/pg.env"; return 0; fi
  kubectl -n data create secret generic pg-burnrate --type=kubernetes.io/basic-auth \
    --from-literal=username=burnrate \
    --from-file=password=<(printf '%s' "$(env_get "$PG_ENV" PG_PASSWORD)") \
    --dry-run=client -o yaml \
    | kubectl label --local -f - cnpg.io/reload=true -o yaml \
    | kubectl apply -f - >/dev/null
  info "secret data/pg-burnrate applied"
}

# Public address. The load balancer IP is the address of Gateway edge/burnrate.
lb_ip() {
  if [[ -n "${LB_IP:-}" ]]; then echo "$LB_IP"; return 0; fi
  if dry; then echo "203.0.113.10"; return 0; fi   # documentation address, stands in for the real one
  kubectl -n edge get gateway burnrate -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true
}
# burnrate.<ip-with-dashes>.sslip.io (or $HOST when set)
public_host() {
  if [[ -n "${HOST:-}" ]]; then echo "$HOST"; return 0; fi
  local ip; ip=$(lb_ip)
  [[ -n "$ip" ]] || die "the load balancer has no IP yet (bootstrap.sh gateway), or pass HOST=..."
  echo "burnrate.${ip//./-}.sslip.io"
}

# wait_pods <namespace> <label-selector> [timeout]
wait_pods() {
  local ns=$1 sel=$2 t=${3:-600s} i
  if dry; then info "[dry-run] wait for pods $ns $sel"; return 0; fi
  for i in $(seq 1 60); do
    [[ -n "$(kubectl -n "$ns" get pods -l "$sel" -o name 2>/dev/null)" ]] && break
    sleep 5
  done
  kubectl -n "$ns" wait --for=condition=Ready pod -l "$sel" --timeout="$t" >/dev/null
  info "pods ready: $ns $sel"
}

has_crd() { kubectl get crd "$1" >/dev/null 2>&1; }

# wait_job <namespace> <job-name> <timeout-seconds>  -> 0 complete, 1 failed or timed out
wait_job() {
  local ns=$1 job=$2 left=$3 st
  while (( left > 0 )); do
    st=$(kubectl -n "$ns" get "$job" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}' 2>/dev/null || true)
    case " $st " in
      *" Complete "*) return 0 ;;
      *" Failed "*) return 1 ;;
    esac
    sleep 3; left=$(( left - 3 ))
  done
  return 1
}

# Image tag of the working tree: short commit id, "-dirty" when something is not committed.
git_tag() {
  local sha
  sha=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null) || die "not a git repository (pass --tag)"
  if [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then sha="$sha-dirty"; fi
  echo "$sha"
}

state_put() { mkdir -p "$STATE_DIR"; printf '%s\n' "$2" > "$STATE_DIR/$1"; }
state_get() { [[ -f "$STATE_DIR/$1" ]] && cat "$STATE_DIR/$1" || true; }
