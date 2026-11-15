#!/usr/bin/env bash
# Rolls the application out: hostnames from the load balancer IP, schema job, apply, wait, print the URL.
#
#   deploy/scripts/deploy.sh                       # the tag build.sh pushed last (deploy/.state/image-tag)
#   deploy/scripts/deploy.sh --tag 3f2a9c1         # a specific image tag; this is also how to roll BACK
#   deploy/scripts/deploy.sh --dry-run             # render everything into a directory and stop: no cluster needed
#   deploy/scripts/deploy.sh --history             # what was deployed when
#   PROFILE=full deploy/scripts/deploy.sh
#   deploy/scripts/deploy.sh --help
#
# Steps
#   1. regenerate the per-service manifests from the code (gen-apps.sh)
#   2. write deploy/apps/env/kustomization.yaml: hostname burnrate.<LB-IP-with-dashes>.sslip.io, image tags,
#      a hash of the configuration (a changed ConfigMap or Secret then rolls every Deployment)
#   3. render deploy/apps/$PROFILE with kustomize
#   4. apply the ConfigMap and the NetworkPolicies, then run the schema Job with the NEW image and wait for it;
#      a failed Job stops the release here, with the old pods still serving
#   5. apply everything else and wait for every Deployment to finish rolling
#   6. print the URL
#
# Options: --host NAME (instead of the name derived from the load balancer), --skip-schema, --timeout SECONDS
# (for all rollouts together, default 900), --no-gen (do not regenerate manifests).
# Environment: PROFILE, REGISTRY / REGISTRY_NAME, TAG, HOST, LB_IP.
# A Deployment whose service was removed from the code is not deleted: kubectl -n burnrate delete deploy,svc <name>.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

TAG=${TAG:-}; SKIP_SCHEMA=0; TIMEOUT=900; GEN=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG=${2:-}; [[ -n "$TAG" ]] || die "--tag needs a value"; shift ;;
    --host) HOST=${2:-}; [[ -n "$HOST" ]] || die "--host needs a value"; shift ;;
    --timeout) TIMEOUT=${2:-}; [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout needs seconds"; shift ;;
    --skip-schema) SKIP_SCHEMA=1 ;;
    --no-gen) GEN=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --history) [[ -f "$STATE_DIR/history" ]] && cat "$STATE_DIR/history" || echo "no deployments recorded yet"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
check_profile
need kubectl
require_context

[[ -n "$TAG" ]] || TAG=$(state_get image-tag)
if [[ -z "$TAG" ]]; then
  dry || die "no image tag: run deploy/scripts/build.sh first, or pass --tag"
  TAG=latest
fi
HOST=$(public_host)
API_IMAGE=$REGISTRY/api
WEB_IMAGE=$REGISTRY/web
OVERLAY=$APPS_DIR/$PROFILE

log "deploy: profile $PROFILE, tag $TAG, https://$HOST$(dry && echo '   (DRY RUN)')"

# ---- 1. manifests from the code ---------------------------------------------------------------------------------
if dry; then
  # a dry run changes no file: only say whether the generated manifests match the code
  "$SCRIPT_DIR/gen-apps.sh" --check --quiet || warn "rendering the manifests as they are on disk"
elif [[ $GEN -eq 1 ]]; then
  "$SCRIPT_DIR/gen-apps.sh" --quiet
  info "manifests regenerated from burnrate/services/__init__.py (details: deploy/apps/GENERATED.txt)"
fi
if grep -q '^workers without topics ([1-9]' "$APPS_DIR/GENERATED.txt" 2>/dev/null; then
  warn "$(grep '^workers without topics' "$APPS_DIR/GENERATED.txt"): these pods will crash-loop until the code subscribes them to a topic"
fi

# ---- 2. what belongs to this deployment --------------------------------------------------------------------------
secret_version=none
if ! dry; then
  kubectl -n "$APP_NS" get secret burnrate-secrets >/dev/null 2>&1 \
    || die "Secret $APP_NS/burnrate-secrets does not exist: run deploy/scripts/secrets.sh first"
  secret_version=$(kubectl -n "$APP_NS" get secret burnrate-secrets -o jsonpath='{.metadata.resourceVersion}')
fi
config_hash=$( { cat "$APPS_DIR/common/configmap.yaml"; echo "$HOST $PROFILE $secret_version"; } | openssl dgst -sha256 | awk '{print substr($NF, 1, 16)}')

ENV_FILE_OUT=$APPS_DIR/env/kustomization.yaml
if dry; then
  # a dry run must not change the working tree: render from a copy
  WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
  cp -R "$APPS_DIR" "$WORK/apps"
  ENV_FILE_OUT=$WORK/apps/env/kustomization.yaml
  OVERLAY=$WORK/apps/$PROFILE
fi
cat > "$ENV_FILE_OUT" <<EOF
# What belongs to ONE deployment of the application: public hostname, image tags, configuration hash.
# REWRITTEN by deploy/scripts/deploy.sh on every run (this version: $(date -u +%Y-%m-%dT%H:%M:%SZ), profile $PROFILE).
# Included by ../small and ../full.
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
images:
  - name: registry.digitalocean.com/burnrate/api
    newName: $API_IMAGE
    newTag: "$TAG"
  - name: registry.digitalocean.com/burnrate/web
    newName: $WEB_IMAGE
    newTag: "$TAG"
patches:
  - target:
      kind: HTTPRoute
      name: burnrate
    patch: |-
      - op: replace
        path: /spec/hostnames/0
        value: $HOST
  - target:
      kind: ConfigMap
      name: burnrate-config
    patch: |-
      - op: replace
        path: /data/BR_PUBLIC_URL
        value: https://$HOST
  # a new hash rolls every Deployment, so a changed ConfigMap or Secret reaches the pods
  - target:
      kind: Deployment
    patch: |-
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: all
      spec:
        template:
          metadata:
            annotations:
              burnrate.io/config-hash: "$config_hash"
EOF

# ---- 3. render -------------------------------------------------------------------------------------------------------
OUT=$(mktemp -d)
if dry; then trap 'rm -rf "$WORK"' EXIT; else trap 'rm -rf "$OUT"' EXIT; fi
kubectl kustomize "$OVERLAY" > "$OUT/all.yaml" || die "kustomize failed for deploy/apps/$PROFILE"
# split: ConfigMap + NetworkPolicy first (the schema Job needs them), the rest after the Job
awk -v pre="$OUT/pre.yaml" -v rest="$OUT/rest.yaml" '
  function flush() { if (doc != "") { f = (kind == "ConfigMap" || kind == "NetworkPolicy") ? pre : rest; printf "%s---\n", doc >> f } doc = ""; kind = "" }
  /^---$/ { flush(); next }
  /^kind: / { kind = $2 }
  { doc = doc $0 "\n" }
  END { flush() }' "$OUT/all.yaml"
touch "$OUT/pre.yaml" "$OUT/rest.yaml"
# the schema Job: same template for both profiles, with the image of this release
sed -e "s#image: .*/api:.*#image: \"$API_IMAGE:$TAG\"#" "$APPS_DIR/jobs/schema.yaml" > "$OUT/schema-job.yaml"
grep -q "image: \"$API_IMAGE:$TAG\"" "$OUT/schema-job.yaml" || die "could not set the image in deploy/apps/jobs/schema.yaml"

n_deploy=$(grep -c '^kind: Deployment$' "$OUT/all.yaml" || true)
info "rendered $(grep -c '^kind: ' "$OUT/all.yaml") objects, $n_deploy Deployments"

if dry; then
  info "[dry-run] rendered files are in $OUT:"
  info "    pre.yaml         $(grep -c '^kind: ' "$OUT/pre.yaml") objects applied before the schema Job (ConfigMap, NetworkPolicies)"
  info "    schema-job.yaml  the Job that runs SERVICE=schema with $API_IMAGE:$TAG"
  info "    rest.yaml        $(grep -c '^kind: ' "$OUT/rest.yaml") objects applied after it"
  info "[dry-run] images: $(grep -E '^ +(- )?image: ' "$OUT/all.yaml" | sed -E 's/^ +(- )?image: //' | sort -u | tr '\n' ' ')"
  info "[dry-run] nothing was applied and the working tree was not changed"
  exit 0
fi

# ---- 4. configuration, then the schema Job ------------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && [[ "${SKIP_IMAGE_CHECK:-0}" != "1" ]]; then
  for img in "$API_IMAGE:$TAG" "$WEB_IMAGE:$TAG"; do
    docker buildx imagetools inspect "$img" >/dev/null 2>&1 \
      || warn "cannot see $img in the registry from this machine (not pushed, or docker is not logged in): pods may fail to pull it"
  done
fi
kubectl apply -f "$OUT/pre.yaml" >/dev/null
info "ConfigMap and NetworkPolicies applied"

if [[ $SKIP_SCHEMA -eq 1 ]]; then
  warn "schema Job skipped (--skip-schema)"
else
  job=$(kubectl create -f "$OUT/schema-job.yaml" -o name)
  info "schema Job $job started (creates missing tables in every service database)..."
  if wait_job "$APP_NS" "$job" 600; then
    info "schema ready"
  else
    kubectl -n "$APP_NS" logs "$job" --tail=40 2>/dev/null | sed 's/^/      /' || true
    die "schema Job failed or did not finish in 10 minutes; NOTHING else was changed, the old pods keep serving.
       kubectl -n $APP_NS describe $job; kubectl -n $APP_NS logs $job"
  fi
fi

# ---- 5. everything else, wait for the rollouts ---------------------------------------------------------------------------
kubectl apply -f "$OUT/rest.yaml" >/dev/null
info "applied; waiting for $n_deploy Deployments to roll (up to ${TIMEOUT}s in total)..."
failed=()
deadline=$(( $(date +%s) + TIMEOUT ))
for d in $(kubectl -n "$APP_NS" get deployment -l app.kubernetes.io/part-of=burnrate -o name); do
  left=$(( deadline - $(date +%s) ))
  (( left < 5 )) && left=5      # they roll in parallel: once the time is up, each one gets a last quick look
  if ! kubectl -n "$APP_NS" rollout status "$d" --timeout="${left}s" >/dev/null 2>&1; then
    failed+=("${d#deployment.apps/}")
  fi
done
mkdir -p "$STATE_DIR"
printf '%s  profile=%s  tag=%s  host=%s  result=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PROFILE" "$TAG" "$HOST" \
  "$( [[ ${#failed[@]} -eq 0 ]] && echo ok || echo "not-rolled:${failed[*]}" )" >> "$STATE_DIR/history"
state_put host "$HOST"
if [[ ${#failed[@]} -gt 0 ]]; then
  kubectl -n "$APP_NS" get pods --no-headers 2>/dev/null | grep -v -E 'Running|Completed' | sed 's/^/      /' || true
  die "these Deployments did not finish rolling: ${failed[*]}
       kubectl -n $APP_NS describe pod <pod>; kubectl -n $APP_NS logs deploy/<name>
       Back to the previous release: deploy/scripts/deploy.sh --tag <previous tag>   (see --history)"
fi

# ---- 6. URL -----------------------------------------------------------------------------------------------------------------
if ! kubectl -n edge wait --for=condition=Ready certificate/burnrate-tls --timeout=5s >/dev/null 2>&1; then
  warn "the TLS certificate edge/burnrate-tls is not ready: https will fail until it is (bootstrap.sh certificate)"
fi
log "deployed tag $TAG"
info "https://$HOST"
info "check it: deploy/scripts/smoke.sh"
