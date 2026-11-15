#!/usr/bin/env bash
# Installs the platform of the Burn Rate cluster for the chosen PROFILE (default: small).
#
#   deploy/scripts/bootstrap.sh                  # every step of the profile, in order (10-15 minutes when new)
#   deploy/scripts/bootstrap.sh kafka postgres   # only the named steps
#   deploy/scripts/bootstrap.sh --list           # the steps of the profile
#   deploy/scripts/bootstrap.sh --dry-run        # touch nothing: render every chart with its values (helm
#                                                # template) and print what would be applied
#   PROFILE=full deploy/scripts/bootstrap.sh
#
# Idempotent: every step is `helm upgrade --install` or `kubectl apply`, and a password is generated only when
# its file in deploy/secrets/ does not exist yet. Secret values are never printed.
#
# Profile small (2 nodes): Envoy Gateway on plain HTTP behind the Caddy edge server, metrics-server, Kafka,
# PostgreSQL, Valkey, KEDA, Prometheus + Grafana, NetworkPolicies. No cert-manager and no Linkerd: TLS ends at
# Caddy, and the mesh does not fit the memory of two nodes (numbers in deploy/README.md).
# Profile full adds cert-manager + certificate (TLS at the gateway), Linkerd, Loki, Tempo, Alloy, Argo CD, Vault.
#
# Environment:
#   PROFILE=small|full     footprint (deploy/README.md)
#   NETPOL=0               do not apply the NetworkPolicies of the data namespace
#   (full) LINKERD=0             leave the service mesh out
#   (full) ISSUER=letsencrypt-staging   when Let's Encrypt production is rate limited
#   (full) FORCE_LINKERD=1       upgrade an installed Linkerd (regenerates its webhook certificates)
#   (full) VAULT_AUTO_INIT=1     initialise and unseal Vault, keep the keys in deploy/secrets/vault-init.json
#   (full) ARGOCD_REPO_URL=https://github.com/<owner>/<repo>   apply the Argo CD Application
# Needs: kubectl (context = the burnrate cluster), helm, doctl, openssl, jq; in full also `step` (Linkerd certificates).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
P=$PLATFORM_DIR
ISSUER=${ISSUER:-letsencrypt-prod}

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

STEPS_SMALL=(namespaces registry gateway metrics_server kafka postgres valkey keda observability netpol)
STEPS_FULL=(namespaces registry gateway metrics_server cert_manager certificate kafka postgres valkey keda
            observability linkerd netpol argocd vault)

repos() {
  helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
  helm repo add strimzi https://strimzi.io/charts/ >/dev/null 2>&1 || true
  helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
  if [[ "$PROFILE" == "full" ]]; then
    helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
    helm repo add linkerd-edge https://helm.linkerd.io/edge >/dev/null 2>&1 || true
    helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
    helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
    helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
  fi
  helm repo update >/dev/null 2>&1 || warn "helm repo update failed; using the local chart index"
}

# ---- namespaces ----------------------------------------------------------------------------------------------
step_namespaces() {
  log "namespaces"
  kapply -f "$P/namespaces/namespaces.yaml"
  info "burnrate (may attach routes to the gateway), edge, data, platform, observability"
}

# ---- registry: let the cluster pull from registry.digitalocean.com/$REGISTRY_NAME ------------------------------
step_registry() {
  log "registry integration ($REGISTRY)"
  if dry; then info "[dry-run] doctl kubernetes cluster registry add $CLUSTER"; return 0; fi
  need doctl
  load_do_token
  doctl kubernetes cluster registry add "$CLUSTER" >/dev/null
  info "DOKS puts the pull secret '$REGISTRY_NAME' into every namespace and on the default service accounts"
}

# ---- gateway: Envoy Gateway + the public Gateway + the DigitalOcean load balancer --------------------------------
gateway_preflight() {
  # Which Gateway API CRDs does the cluster already have (DOKS installs them with Cilium), and is another
  # Gateway controller present? Nothing is changed here; the answer is printed because it explains failures.
  local bundle classes
  bundle=$(kubectl get crd gateways.gateway.networking.k8s.io \
    -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version} ({.metadata.annotations.gateway\.networking\.k8s\.io/channel})' 2>/dev/null || true)
  if [[ -n "$bundle" ]]; then
    info "Gateway API CRDs already in the cluster: $bundle. Envoy Gateway $V_ENVOY_GATEWAY is built for v1.6.1;"
    info "Helm adds the CRDs that are missing and leaves the existing ones as they are."
    case "$bundle" in
      v1.0*|v1.1.*|v0.*) warn "these CRDs are older than v1.2: HTTPRoute.spec.rules[].timeouts (used by deploy/apps/common/routes.yaml) may be rejected" ;;
    esac
  else
    info "no Gateway API CRDs in the cluster yet: the Envoy Gateway chart installs v1.6.1"
  fi
  classes=$(kubectl get gatewayclass -o jsonpath='{range .items[*]}{.metadata.name}={.spec.controllerName} {end}' 2>/dev/null || true)
  [[ -n "$classes" ]] && info "GatewayClasses before install: $classes"
  return 0
}

# Profile small: what has to be on the edge server for the public name to reach this cluster.
edge_hint() {
  info "Caddy on the droplet burnrate-app ($EDGE_IP) must forward to it. In its Caddyfile:"
  info "    $EDGE_HOST {"
  info "        reverse_proxy http://$1:80"
  info "    }"
  info "then: sudo systemctl reload caddy     (details: deploy/README.md, \"The edge server\")"
}

step_gateway() {
  log "Envoy Gateway $V_ENVOY_GATEWAY + Gateway edge/burnrate (profile $PROFILE)"
  dry || gateway_preflight
  helm_up eg envoy-gateway-system oci://docker.io/envoyproxy/gateway-helm "$V_ENVOY_GATEWAY" "$P/envoy-gateway/values.yaml"
  kapply -f "$P/envoy-gateway/proxy-$PROFILE.yaml" -f "$P/envoy-gateway/gateway-$PROFILE.yaml"
  if dry; then info "[dry-run] wait for the load balancer IP of Gateway edge/burnrate"; return 0; fi
  info "waiting for the DigitalOcean load balancer (2-4 minutes)..."
  local ip=""
  for _ in $(seq 1 60); do
    ip=$(lb_ip)
    [[ -n "$ip" ]] && break
    sleep 10
  done
  [[ -n "$ip" ]] || die "Gateway edge/burnrate has no address after 10 minutes: kubectl -n edge describe gateway burnrate; kubectl -n envoy-gateway-system get pods,svc"
  state_put lb-ip "$ip"
  if [[ "$PROFILE" == "small" ]]; then
    info "load balancer $LB_NAME: $ip (plain HTTP on port 80)"
    edge_hint "$ip"
  else
    info "load balancer $LB_NAME: $ip  ->  https://$(public_host)"
  fi
}

step_metrics_server() {
  log "metrics-server (kubectl top, HorizontalPodAutoscaler)"
  helm_up metrics-server kube-system metrics-server/metrics-server "$V_METRICS_SERVER" "$P/metrics-server/values.yaml"
}

# ---- cert-manager + Let's Encrypt -------------------------------------------------------------------------------
step_cert_manager() {
  log "cert-manager $V_CERT_MANAGER"
  helm_up cert-manager cert-manager jetstack/cert-manager "$V_CERT_MANAGER" "$P/cert-manager/values.yaml"
  kapply -f "$P/cert-manager/clusterissuers.yaml"
  info "ClusterIssuers letsencrypt-prod, letsencrypt-staging (HTTP-01 through Gateway edge/burnrate)"
}

step_certificate() {
  local host; host=$(public_host)
  log "certificate for $host ($ISSUER)"
  if dry; then
    sed -e "s/__HOST__/$host/g" -e "s/__ISSUER__/$ISSUER/g" "$P/cert-manager/certificate.tmpl.yaml" >/dev/null
    info "[dry-run] kubectl apply Certificate edge/burnrate-tls for $host"
    return 0
  fi
  sed -e "s/__HOST__/$host/g" -e "s/__ISSUER__/$ISSUER/g" "$P/cert-manager/certificate.tmpl.yaml" | kubectl apply -f - >/dev/null
  state_put host "$host"
  info "waiting for Let's Encrypt (HTTP-01, usually under 2 minutes)..."
  if kubectl -n edge wait --for=condition=Ready certificate/burnrate-tls --timeout=300s >/dev/null 2>&1; then
    info "certificate edge/burnrate-tls issued"
  else
    warn "certificate not ready yet. Look at: kubectl -n edge describe certificate burnrate-tls; kubectl -n edge get challenges,orders,httproute"
    warn "if Let's Encrypt answers 'too many certificates': ISSUER=letsencrypt-staging $0 certificate"
  fi
}

# ---- Strimzi + Kafka ------------------------------------------------------------------------------------------------
step_kafka() {
  log "Strimzi $V_STRIMZI + Kafka 'burnrate' (profile $PROFILE)"
  helm_up strimzi platform strimzi/strimzi-kafka-operator "$V_STRIMZI" "$P/strimzi/values.yaml"
  kapply -f "$P/strimzi/kafka-$PROFILE.yaml"
  dry || info "waiting for Kafka to become Ready (first start takes 2-4 minutes)..."
  kwait -n data kafka/burnrate --for=condition=Ready --timeout=900s
  if [[ "$PROFILE" == "full" ]]; then
    kapply -f "$P/strimzi/topics.yaml"
    kwait -n data kafkatopic --all --for=condition=Ready --timeout=300s
    info "declared topics: $(grep -c '^kind: KafkaTopic' "$P/strimzi/topics.yaml")"
  else
    info "topics are created on first use (3 partitions, replication 1)"
  fi
  info "Kafka at burnrate-kafka-bootstrap.data:9092"
}

# ---- CloudNativePG + PostgreSQL ----------------------------------------------------------------------------------------
step_postgres() {
  log "CloudNativePG $V_CNPG + PostgreSQL 17 cluster 'pg' (profile $PROFILE)"
  helm_up cnpg platform cnpg/cloudnative-pg "$V_CNPG" "$P/cnpg/values.yaml"
  apply_pg_secret
  kapply -f "$P/cnpg/cluster-$PROFILE.yaml"
  dry || info "waiting for the cluster (initdb, then the replicas: 3-5 minutes)..."
  kwait -n data cluster.postgresql.cnpg.io/pg --for=condition=Ready --timeout=900s
  kapply -f "$P/cnpg/databases.yaml"
  kwait -n data database.postgresql.cnpg.io --all --for=jsonpath='{.status.applied}'=true --timeout=300s
  info "PostgreSQL at pg-rw.data:5432, $(grep -c '^kind: Database' "$P/cnpg/databases.yaml") databases owned by role burnrate"
}

step_valkey() {
  log "Valkey"
  kapply -f "$P/valkey/valkey.yaml"
  wait_pods data app.kubernetes.io/name=valkey 300s
  info "Valkey at valkey.data:6379"
}

step_keda() {
  log "KEDA $V_KEDA"
  helm_up keda platform kedacore/keda "$V_KEDA" "$P/keda/values.yaml"
}

# ---- observability -----------------------------------------------------------------------------------------------------------
step_observability() {
  log "observability (profile $PROFILE)"
  ensure_env_file "$SECRETS_DIR/grafana.env" admin-user=admin admin-password=@rand
  secret_from_env_file observability grafana-admin "$SECRETS_DIR/grafana.env"
  helm_up kps observability prometheus-community/kube-prometheus-stack "$V_KPS" \
    "$P/kube-prometheus-stack/values.yaml" -f "$P/kube-prometheus-stack/values-$PROFILE.yaml"
  if [[ "$PROFILE" == "full" ]]; then
    helm_up loki observability grafana/loki "$V_LOKI" "$P/loki/values.yaml"
    helm_up tempo observability grafana/tempo "$V_TEMPO" "$P/tempo/values.yaml"
    helm_up alloy observability grafana/alloy "$V_ALLOY" "$P/alloy/values.yaml"
    kapply -f "$P/linkerd/podmonitor.yaml" -f "$P/kube-prometheus-stack/monitors-full.yaml"
  fi
  kapply -f "$P/kube-prometheus-stack/monitors.yaml"
  kapply -k "$P/grafana-dashboards"
  info "Grafana: kubectl -n observability port-forward svc/kps-grafana 3000:80   (admin / deploy/secrets/grafana.env)"
}

# ---- Linkerd (edge) ------------------------------------------------------------------------------------------------------------
linkerd_certs() {
  local L=$SECRETS_DIR/linkerd
  [[ -f "$L/issuer.key" ]] && return 0
  need step
  mkdir -p "$L"; chmod 700 "$L"
  step certificate create root.linkerd.cluster.local "$L/ca.crt" "$L/ca.key" \
    --profile root-ca --no-password --insecure --not-after=8760h >/dev/null 2>&1
  step certificate create identity.linkerd.cluster.local "$L/issuer.crt" "$L/issuer.key" \
    --profile intermediate-ca --not-after=8760h --no-password --insecure \
    --ca "$L/ca.crt" --ca-key "$L/ca.key" >/dev/null 2>&1
  chmod 600 "$L"/*
  info "generated Linkerd trust anchor and issuer in deploy/secrets/linkerd/ (valid 1 year)"
}

step_linkerd() {
  if [[ "${LINKERD:-1}" == "0" ]]; then
    log "Linkerd: skipped (LINKERD=0)"
    dry || kubectl annotate ns "$APP_NS" linkerd.io/inject=disabled --overwrite >/dev/null 2>&1 || true
    return 0
  fi
  log "Linkerd edge $V_LINKERD"
  local extra=()
  [[ "$PROFILE" == "full" ]] && extra=(-f "$P/linkerd/values-full.yaml")
  if dry; then
    helm_up linkerd-crds linkerd linkerd-edge/linkerd-crds "$V_LINKERD" "$P/linkerd/crds-values.yaml"
    # certificates are generated on a real run; a throw-away PEM-shaped placeholder is enough to render
    local T; T=$(mktemp -d)
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=root.linkerd.cluster.local" \
      -keyout "$T/k.pem" -out "$T/c.pem" >/dev/null 2>&1
    helm_up linkerd-control-plane linkerd linkerd-edge/linkerd-control-plane "$V_LINKERD" "$P/linkerd/values.yaml" \
      ${extra[@]+"${extra[@]}"} \
      --set-file identityTrustAnchorsPEM="$T/c.pem" \
      --set-file identity.issuer.tls.crtPEM="$T/c.pem" \
      --set-file identity.issuer.tls.keyPEM="$T/k.pem"
    rm -f "$T/k.pem" "$T/c.pem"; rmdir "$T"
    return 0
  fi
  linkerd_certs
  local L=$SECRETS_DIR/linkerd
  helm_up linkerd-crds linkerd linkerd-edge/linkerd-crds "$V_LINKERD" "$P/linkerd/crds-values.yaml"
  # The chart regenerates its webhook certificates on every upgrade: only upgrade on request.
  if helm -n linkerd status linkerd-control-plane >/dev/null 2>&1 && [[ "${FORCE_LINKERD:-0}" != "1" ]]; then
    info "linkerd-control-plane is installed (FORCE_LINKERD=1 to upgrade it)"
  else
    helm_up linkerd-control-plane linkerd linkerd-edge/linkerd-control-plane "$V_LINKERD" "$P/linkerd/values.yaml" \
      ${extra[@]+"${extra[@]}"} \
      --set-file identityTrustAnchorsPEM="$L/ca.crt" \
      --set-file identity.issuer.tls.crtPEM="$L/issuer.crt" \
      --set-file identity.issuer.tls.keyPEM="$L/issuer.key"
  fi
  local d
  for d in linkerd-destination linkerd-identity linkerd-proxy-injector; do
    kubectl -n linkerd rollout status "deploy/$d" --timeout=300s >/dev/null
  done
  kubectl annotate ns "$APP_NS" linkerd.io/inject=enabled --overwrite >/dev/null
  info "Linkerd ready; pods created in namespace $APP_NS from now on get a proxy"
}

# ---- NetworkPolicies of the data namespace ------------------------------------------------------------------------------------------
step_netpol() {
  if [[ "${NETPOL:-1}" == "0" ]]; then log "NetworkPolicies: skipped (NETPOL=0)"; return 0; fi
  log "NetworkPolicies of namespace data (default deny, the application namespace allowed)"
  kapply -f "$P/network-policies/data.yaml"
  dry && return 0
  # A policy that is too tight shows first as an operator that cannot reach what it manages.
  sleep 20
  local pg kafka
  pg=$(kubectl -n data get cluster.postgresql.cnpg.io/pg -o jsonpath='{.status.phase}' 2>/dev/null || true)
  kafka=$(kubectl -n data get kafka/burnrate -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  info "after applying: PostgreSQL '$pg', Kafka Ready=$kafka"
  [[ "$kafka" == "True" ]] || warn "Kafka is not Ready: kubectl -n data describe kafka burnrate"
  [[ "$pg" == *healthy* ]] || warn "PostgreSQL is not healthy: kubectl -n data describe cluster pg   (to undo: kubectl delete -f deploy/platform/network-policies/data.yaml)"
}

# ---- Argo CD (full) ---------------------------------------------------------------------------------------------------------------------
step_argocd() {
  log "Argo CD (profile full)"
  helm_up argocd argocd argo/argo-cd "$V_ARGOCD" "$P/argocd/values.yaml"
  if dry; then info "[dry-run] Application burnrate-apps: ${ARGOCD_REPO_URL:-not applied (ARGOCD_REPO_URL is not set)}"; return 0; fi
  if [[ ! -f "$SECRETS_DIR/argocd.env" ]] && kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
    local old_umask; old_umask=$(umask); umask 077
    { printf 'ARGOCD_USER=admin\nARGOCD_PASSWORD='
      kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 --decode
      printf '\n'; } > "$SECRETS_DIR/argocd.env"
    chmod 600 "$SECRETS_DIR/argocd.env"; umask "$old_umask"
    info "Argo CD admin password saved to deploy/secrets/argocd.env"
  fi
  if [[ -n "${ARGOCD_REPO_URL:-}" ]]; then
    sed -e "s#__REPO_URL__#$ARGOCD_REPO_URL#g" -e "s#__REVISION__#${ARGOCD_REVISION:-main}#g" \
      "$P/argocd/application.tmpl.yaml" | kubectl apply -f - >/dev/null
    info "Application argocd/burnrate-apps applied (manual sync; path deploy/apps/full)"
  else
    info "Application not applied: set ARGOCD_REPO_URL=https://github.com/<owner>/<repo> and re-run this step"
  fi
  info "UI: kubectl -n argocd port-forward svc/argocd-server 8080:80"
}

# ---- Vault, Raft, 3 pods (full) -----------------------------------------------------------------------------------------------------------
step_vault() {
  log "Vault (profile full: Raft, 3 pods)"
  if dry; then
    helm_up vault platform hashicorp/vault "$V_VAULT" "$P/vault/values.yaml"
    return 0
  fi
  # no --wait: a sealed Vault is not Ready, and it stays sealed until a person (or VAULT_AUTO_INIT) unseals it
  helm upgrade --install vault hashicorp/vault -n platform --version "$V_VAULT" -f "$P/vault/values.yaml" >/dev/null
  info "helm release platform/vault deployed"
  for _ in $(seq 1 60); do
    [[ "$(kubectl -n platform get pod vault-0 -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && break
    sleep 5
  done
  local init="$SECRETS_DIR/vault-init.json"
  if kubectl -n platform exec vault-0 -- vault status -format=json 2>/dev/null | jq -e '.initialized' >/dev/null 2>&1; then
    info "Vault is already initialised"
  elif [[ "${VAULT_AUTO_INIT:-0}" == "1" ]]; then
    local old_umask; old_umask=$(umask); umask 077
    kubectl -n platform exec vault-0 -- vault operator init -key-shares=5 -key-threshold=3 -format=json > "$init"
    chmod 600 "$init"; umask "$old_umask"
    info "Vault initialised; unseal keys and root token are in deploy/secrets/vault-init.json (move them somewhere safer)"
  else
    info "Vault is installed but NOT initialised. Either re-run with VAULT_AUTO_INIT=1, or by hand:"
    info "  kubectl -n platform exec -it vault-0 -- vault operator init          # keep the 5 keys and the root token"
    info "  for p in vault-0 vault-1 vault-2: kubectl -n platform exec -it \$p -- vault operator unseal   (3 keys each)"
    return 0
  fi
  if [[ -f "$init" ]]; then
    local p k
    for p in vault-0 vault-1 vault-2; do
      for k in 0 1 2; do
        kubectl -n platform exec "$p" -- vault operator unseal "$(jq -r ".unseal_keys_b64[$k]" "$init")" >/dev/null 2>&1 || true
      done
    done
    info "unseal sent to vault-0..2: kubectl -n platform get pods -l app.kubernetes.io/name=vault"
  else
    info "Vault pods restart sealed: unseal each with 3 of the keys you kept"
  fi
}

# ---- main ---------------------------------------------------------------------------------------------------------------------------------
main() {
  local steps=() arg list=0
  for arg in "$@"; do
    case "$arg" in
      -h|--help) usage; exit 0 ;;
      --list) list=1 ;;
      --dry-run) DRY_RUN=1 ;;
      -*) usage >&2; die "unknown option: $arg" ;;
      *) steps+=("$arg") ;;
    esac
  done
  check_profile
  local all=("${STEPS_SMALL[@]}")
  [[ "$PROFILE" == "full" ]] && all=("${STEPS_FULL[@]}")
  if [[ $list -eq 1 ]]; then printf '%s\n' "${all[@]}"; exit 0; fi

  need helm
  if dry; then
    log "DRY RUN, profile $PROFILE: nothing is installed. Charts are rendered with their values; the load balancer IP is a stand-in."
  else
    need kubectl openssl jq
    require_context
    mkdir -p "$SECRETS_DIR" "$STATE_DIR"; chmod 700 "$SECRETS_DIR"
  fi
  repos
  [[ ${#steps[@]} -eq 0 ]] && steps=("${all[@]}")
  local s
  for s in "${steps[@]}"; do
    declare -F "step_$s" >/dev/null || die "unknown step: $s (see --list)"
    "step_$s"
  done
  log "done (profile $PROFILE): ${steps[*]}"
  if ! dry; then
    if [[ "$PROFILE" == "small" && -n "$(state_get lb-ip)" ]]; then edge_hint "$(state_get lb-ip)"; fi
    info "next: deploy/scripts/secrets.sh, deploy/scripts/build.sh, deploy/scripts/deploy.sh, deploy/scripts/smoke.sh"
  fi
}
main "$@"
