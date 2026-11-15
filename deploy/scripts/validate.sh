#!/usr/bin/env bash
# Checks everything under deploy/ that can be checked WITHOUT a cluster. Touches no cloud resource.
#
#   deploy/scripts/validate.sh                # quick, offline: script syntax, generated manifests up to date,
#                                             # kustomize renders both profiles, Deployment count = service lists
#   deploy/scripts/validate.sh --charts       # + render every Helm chart of both profiles with its values
#   deploy/scripts/validate.sh --schemas      # + check every custom resource against the CRD schemas of the charts
#   deploy/scripts/validate.sh --budget       # + CPU / memory requests per component for PROFILE (default small)
#   deploy/scripts/validate.sh --all          # all of the above
#   deploy/scripts/validate.sh --keep DIR     # keep the rendered files in DIR
#   deploy/scripts/validate.sh --help
#
# --charts, --schemas and --budget download charts (helm repo add / helm template); --schemas and --budget
# also need `uv` (they run small Python scripts with PyYAML in a throw-away environment).
# The shellcheck linter is used when it is installed.
# What this cannot tell: whether the cluster accepts and runs it. See "Not verified" in deploy/README.md.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

CHARTS=0; SCHEMAS=0; BUDGET=0; KEEP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --charts) CHARTS=1 ;;
    --schemas) SCHEMAS=1 ;;
    --budget) BUDGET=1 ;;
    --all) CHARTS=1; SCHEMAS=1; BUDGET=1 ;;
    --keep) KEEP=${2:-}; [[ -n "$KEEP" ]] || die "--keep needs a directory"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
check_profile
need kubectl python3

if [[ -n "$KEEP" ]]; then OUT=$KEEP; mkdir -p "$OUT"; else OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT; fi
failed=0
ok()  { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; failed=$(( failed + 1 )); }

# ---- 1. scripts ------------------------------------------------------------------------------------------------
log "scripts"
for f in "$SCRIPT_DIR"/*.sh; do
  if bash -n "$f" 2>"$OUT/err.txt"; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f"): $(head -1 "$OUT/err.txt")"; fi
done
if command -v shellcheck >/dev/null 2>&1; then
  if (cd "$SCRIPT_DIR" && shellcheck -x -- *.sh) >"$OUT/shellcheck.txt" 2>&1; then ok "shellcheck"; else bad "shellcheck:"; sed 's/^/        /' "$OUT/shellcheck.txt"; fi
else
  info "shellcheck is not installed: skipped"
fi
for f in "$SCRIPT_DIR"/*.py; do
  if python3 -B -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$f" 2>"$OUT/err.txt"; then ok "python syntax $(basename "$f")"
  else bad "python syntax $(basename "$f"): $(tail -1 "$OUT/err.txt")"; fi
done

# ---- 2. generated manifests -------------------------------------------------------------------------------------
log "manifests"
if "$SCRIPT_DIR/gen-apps.sh" --check --quiet 2>"$OUT/err.txt"; then ok "generated manifests match burnrate/services/__init__.py"
else bad "$(head -1 "$OUT/err.txt")"; fi

expect=$(sed -n 's/^profile small: \([0-9]*\) Deployments.*/\1/p' "$APPS_DIR/GENERATED.txt")
for target in "" small full; do
  name=${target:-default}
  if kubectl kustomize "$APPS_DIR/$target" > "$OUT/apps-$name.yaml" 2>"$OUT/err.txt"; then
    n=$(grep -c '^kind: Deployment$' "$OUT/apps-$name.yaml" || true)
    if [[ "$n" == "$expect" ]]; then ok "kubectl kustomize deploy/apps${target:+/$target}: $(grep -c '^kind: ' "$OUT/apps-$name.yaml") objects, $n Deployments (services + workers + loops + web)"
    else bad "kubectl kustomize deploy/apps${target:+/$target}: $n Deployments, expected $expect"; fi
  else
    bad "kubectl kustomize deploy/apps${target:+/$target}: $(head -2 "$OUT/err.txt" | tr '\n' ' ')"
  fi
done
if cmp -s "$OUT/apps-default.yaml" "$OUT/apps-small.yaml"; then ok "deploy/apps renders the small profile"; else bad "deploy/apps and deploy/apps/small render differently"; fi
if kubectl kustomize "$PLATFORM_DIR/grafana-dashboards" > "$OUT/dashboards.yaml" 2>"$OUT/err.txt"; then ok "kubectl kustomize deploy/platform/grafana-dashboards"
else bad "grafana-dashboards: $(head -1 "$OUT/err.txt")"; fi
for f in "$PLATFORM_DIR"/grafana-dashboards/*.json; do
  if python3 -B -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["panels"] and d["uid"]' "$f" 2>/dev/null; then ok "dashboard JSON $(basename "$f")"
  else bad "dashboard JSON $(basename "$f") does not parse"; fi
done

# ---- 3. Helm charts with our values -------------------------------------------------------------------------------
# release|namespace|chart|version|values files (comma separated, relative to deploy/platform)
charts_of() {
  cat <<EOF
eg|envoy-gateway-system|oci://docker.io/envoyproxy/gateway-helm|$V_ENVOY_GATEWAY|envoy-gateway/values.yaml
metrics-server|kube-system|metrics-server/metrics-server|$V_METRICS_SERVER|metrics-server/values.yaml
cert-manager|cert-manager|jetstack/cert-manager|$V_CERT_MANAGER|cert-manager/values.yaml
strimzi|platform|strimzi/strimzi-kafka-operator|$V_STRIMZI|strimzi/values.yaml
cnpg|platform|cnpg/cloudnative-pg|$V_CNPG|cnpg/values.yaml
keda|platform|kedacore/keda|$V_KEDA|keda/values.yaml
kps|observability|prometheus-community/kube-prometheus-stack|$V_KPS|kube-prometheus-stack/values.yaml,kube-prometheus-stack/values-$1.yaml
linkerd-crds|linkerd|linkerd-edge/linkerd-crds|$V_LINKERD|linkerd/crds-values.yaml
linkerd-control-plane|linkerd|linkerd-edge/linkerd-control-plane|$V_LINKERD|linkerd/values.yaml$( [[ "$1" == full ]] && echo ",linkerd/values-full.yaml" )
EOF
  if [[ "$1" == "full" ]]; then
    cat <<EOF
loki|observability|grafana/loki|$V_LOKI|loki/values.yaml
tempo|observability|grafana/tempo|$V_TEMPO|tempo/values.yaml
alloy|observability|grafana/alloy|$V_ALLOY|alloy/values.yaml
argocd|argocd|argo/argo-cd|$V_ARGOCD|argocd/values.yaml
vault|platform|hashicorp/vault|$V_VAULT|vault/values.yaml
EOF
  fi
}

render_charts() { # $1 = profile -> $OUT/charts-<profile>/<namespace>__<release>.yaml (CRDs included)
  local profile=$1 dir="$OUT/charts-$1" rel ns chart ver values f args
  [[ -d "$dir" ]] && return 0
  mkdir -p "$dir"
  # Linkerd's chart wants its certificates at render time; a throw-away self-signed one will do
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=root.linkerd.cluster.local" \
    -keyout "$OUT/l5d.key" -out "$OUT/l5d.crt" >/dev/null 2>&1
  while IFS='|' read -r rel ns chart ver values; do
    args=()
    IFS=',' read -r -a files <<< "$values"
    for f in "${files[@]}"; do args+=(-f "$PLATFORM_DIR/$f"); done
    if [[ "$rel" == "linkerd-control-plane" ]]; then
      args+=(--set-file "identityTrustAnchorsPEM=$OUT/l5d.crt" --set-file "identity.issuer.tls.crtPEM=$OUT/l5d.crt"
             --set-file "identity.issuer.tls.keyPEM=$OUT/l5d.key")
    fi
    if helm template "$rel" "$chart" -n "$ns" --version "$ver" --include-crds "${args[@]}" < /dev/null > "$dir/${ns}__${rel}.yaml" 2>"$OUT/err.txt"; then
      ok "helm template $chart $ver ($profile): $(grep -c '^kind: ' "$dir/${ns}__${rel}.yaml") objects"
    else
      bad "helm template $chart $ver ($profile): $(grep -v '^Pulled\|^Digest' "$OUT/err.txt" | head -2 | tr '\n' ' ')"
    fi
  done < <(charts_of "$profile")
  rm -f "$OUT/l5d.key"
}

if [[ $CHARTS -eq 1 || $SCHEMAS -eq 1 || $BUDGET -eq 1 ]]; then
  need helm openssl
  log "Helm charts rendered with the values of deploy/platform"
  if [[ $CHARTS -eq 1 || $SCHEMAS -eq 1 ]]; then render_charts small; render_charts full; else render_charts "$PROFILE"; fi
fi

# raw manifests of the platform, with their placeholders filled in
platform_manifests() { # $1 = profile -> file names, one per line
  local profile=$1 dir="$OUT/platform-$1"
  mkdir -p "$dir"
  sed -e "s/__HOST__/burnrate.203-0-113-10.sslip.io/g" -e "s/__ISSUER__/letsencrypt-prod/g" \
    "$PLATFORM_DIR/cert-manager/certificate.tmpl.yaml" > "$dir/certificate.yaml"
  printf '%s\n' "$PLATFORM_DIR/namespaces/namespaces.yaml" "$PLATFORM_DIR/envoy-gateway/gateway.yaml" \
    "$PLATFORM_DIR/envoy-gateway/proxy-$profile.yaml" "$PLATFORM_DIR/cert-manager/clusterissuers.yaml" "$dir/certificate.yaml" \
    "$PLATFORM_DIR/cnpg/cluster-$profile.yaml" "$PLATFORM_DIR/cnpg/databases.yaml" "$PLATFORM_DIR/strimzi/kafka-$profile.yaml" \
    "$PLATFORM_DIR/valkey/valkey.yaml" "$PLATFORM_DIR/network-policies/data.yaml" "$PLATFORM_DIR/kube-prometheus-stack/monitors.yaml"
  if [[ "$profile" == "full" ]]; then
    sed -e "s#__REPO_URL__#https://github.com/example/burnrate#g" -e "s#__REVISION__#main#g" \
      "$PLATFORM_DIR/argocd/application.tmpl.yaml" > "$dir/application.yaml"
    printf '%s\n' "$PLATFORM_DIR/strimzi/topics.yaml" "$PLATFORM_DIR/linkerd/podmonitor.yaml" "$dir/application.yaml"
  fi
}

# ---- 4. custom resources against the CRD schemas ---------------------------------------------------------------------
if [[ $SCHEMAS -eq 1 ]]; then
  need uv
  log "custom resources against the CRD schemas of the pinned charts"
  for profile in small full; do
    manifests=()
    while IFS= read -r f; do manifests+=("$f"); done < <(platform_manifests "$profile")
    manifests+=("$OUT/apps-$profile.yaml" "$APPS_DIR/jobs/schema.yaml")
    if uv run --quiet --no-project --isolated --with pyyaml python -B "$SCRIPT_DIR/check_schemas.py" \
         --crds "$OUT/charts-$profile"/*.yaml -- "${manifests[@]}" > "$OUT/schemas-$profile.txt" 2>&1; then
      ok "profile $profile: $(tail -1 "$OUT/schemas-$profile.txt" | sed 's/^ *//')"
    else
      bad "profile $profile:"; sed 's/^/      /' "$OUT/schemas-$profile.txt"
    fi
  done
fi

# ---- 5. requests -----------------------------------------------------------------------------------------------------------
if [[ $BUDGET -eq 1 ]]; then
  need uv
  log "CPU and memory requests, profile $PROFILE, $NODE_COUNT nodes (Linkerd proxies included)"
  args=()
  for f in "$OUT/charts-$PROFILE"/*.yaml; do b=$(basename "$f" .yaml); args+=("${b%%__*}=$f"); done
  while IFS= read -r f; do args+=("default=$f"); done < <(platform_manifests "$PROFILE")
  args+=("burnrate=$OUT/apps-$PROFILE.yaml")
  uv run --quiet --no-project --isolated --with pyyaml python -B "$SCRIPT_DIR/budget.py" --nodes "$NODE_COUNT" --mesh "${args[@]}" \
    | tee "$OUT/budget-$PROFILE.txt" | sed 's/^/    /'
  info "not in this table: what DigitalOcean runs in kube-system (measured on the previous cluster: about 2 GiB on 4 nodes)"
fi

echo
[[ -n "$KEEP" ]] && info "rendered files kept in $KEEP"
if (( failed > 0 )); then printf '\033[1;31m%d check(s) failed\033[0m\n' "$failed"; exit 1; fi
printf '\033[1;32mall checks passed\033[0m\n'
