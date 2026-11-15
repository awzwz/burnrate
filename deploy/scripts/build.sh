#!/usr/bin/env bash
# Builds both images for linux/amd64 (the nodes; laptops are often arm64) and pushes them to the registry.
#
#   deploy/scripts/build.sh                 # api and web, tags <git short sha> and latest, push
#   deploy/scripts/build.sh api             # only one of: api | web
#   deploy/scripts/build.sh --no-push       # build into the local Docker only (no registry, no login)
#   deploy/scripts/build.sh --dry-run       # print the docker commands, run nothing
#   deploy/scripts/build.sh --tag v1        # another tag instead of the commit id
#   deploy/scripts/build.sh --help
#
# Images:  $REGISTRY/api  (Dockerfile)   and   $REGISTRY/web  (web/Dockerfile); the build context of both is the
# repository root. REGISTRY defaults to registry.digitalocean.com/burnrate (REGISTRY_NAME=... to change the name).
#
# The tag is the short commit id. With uncommitted changes it becomes <sha>-dirty-<UTC time>, so that two
# different builds never share a tag (a Deployment only rolls when its image tag changes).
# The tag is remembered in deploy/.state/image-tag; deploy.sh uses it by default.
# Login: `doctl registry login` with $DIGITALOCEAN_ACCESS_TOKEN or ~/.do_token (SKIP_LOGIN=1 if already logged in).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

PUSH=1; TAG=${TAG:-}; WHAT=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push) PUSH=0 ;;
    --dry-run) DRY_RUN=1 ;;
    --tag) TAG=${2:-}; [[ -n "$TAG" ]] || die "--tag needs a value"; shift ;;
    api|web) WHAT+=("$1") ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
[[ ${#WHAT[@]} -eq 0 ]] && WHAT=(api web)

if [[ -z "$TAG" ]]; then
  TAG=$(git_tag)
  case "$TAG" in *-dirty) TAG="$TAG-$(date -u +%Y%m%dT%H%M%S)" ;; esac
fi
[[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "not a valid image tag: $TAG"
REVISION=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)

run() { if dry; then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

dry || need docker
log "images for linux/amd64, tag $TAG$( [[ $PUSH -eq 1 ]] && echo ', push to '"$REGISTRY" || echo ', local only' )"
case "$TAG" in *-dirty-*) warn "the working tree has uncommitted changes: this image cannot be rebuilt from a commit" ;; esac

if [[ $PUSH -eq 1 && "${SKIP_LOGIN:-0}" != "1" ]]; then
  if dry; then
    info "[dry-run] doctl registry login"
  else
    need doctl
    load_do_token
    doctl registry login >/dev/null
    info "logged in to registry.digitalocean.com"
  fi
fi

for name in "${WHAT[@]}"; do
  case "$name" in
    api) dockerfile=Dockerfile ;;
    web) dockerfile=web/Dockerfile
         [[ -f "$REPO_DIR/web/package.json" ]] || die "web/package.json not found: the front end is not there yet (build only the API: $0 api)" ;;
  esac
  image=$REGISTRY/$name
  args=(buildx build --platform linux/amd64 -f "$REPO_DIR/$dockerfile"
        -t "$image:$TAG" -t "$image:latest"
        --label "org.opencontainers.image.revision=$REVISION"
        --label "org.opencontainers.image.title=burnrate-$name")
  if [[ $PUSH -eq 1 ]]; then args+=(--push); else args+=(--load); fi
  args+=("$REPO_DIR")
  log "$name: $image:$TAG"
  run docker "${args[@]}"
done

if ! dry; then
  state_put image-tag "$TAG"
  [[ $PUSH -eq 1 ]] && info "pushed. Next: deploy/scripts/deploy.sh   (deploys tag $TAG)"
fi
exit 0
