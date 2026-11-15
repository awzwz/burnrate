#!/usr/bin/env bash
# Builds and applies the Secret burnrate/burnrate-secrets, the one Secret every Burn Rate pod reads.
#
#   deploy/scripts/secrets.sh                    # generate what is missing, apply
#   deploy/scripts/secrets.sh --dry-run          # show which keys would be set and where each comes from; apply nothing
#   deploy/scripts/secrets.sh --rotate jwt       # new value for one of: jwt | internal | admin | db | all, then apply
#   deploy/scripts/secrets.sh --restart          # after applying, restart every Deployment so pods read the new values
#   deploy/scripts/secrets.sh --help
#
# Where the values come from
#   generated once, kept in deploy/secrets/app.env (gitignored, chmod 600):
#       BR_JWT_SECRET  BR_INTERNAL_TOKEN  BR_ADMIN_TOKEN
#   built from deploy/secrets/pg.env (the PostgreSQL password, created by bootstrap.sh step postgres):
#       BR_DATABASE_URL = postgresql+asyncpg://burnrate:<password>@pg-rw.data.svc.cluster.local:5432/{service}
#   taken from YOUR environment, or from .env in the repository root when the variable is not exported:
#       BR_OPENAI_API_KEY                                   (required while BR_LLM_PROVIDER=openai)
#       BR_ANTHROPIC_API_KEY  BR_STRIPE_SECRET_KEY  BR_STRIPE_WEBHOOK_SECRET
#       BR_TWILIO_ACCOUNT_SID  BR_TWILIO_AUTH_TOKEN  BR_TWILIO_FROM  BR_SMTP_URL      (optional: set only when present)
#
# No value is ever printed: only key names. The shape of the Secret: deploy/apps/secret.example.yaml.
# Pods read a Secret when they start: after a change run with --restart (or deploy.sh).
#
# Rotation notes
#   jwt       every session is signed out
#   internal  services still running with the old token refuse calls from restarted ones until all have restarted
#   db        the password of role "burnrate" is changed in PostgreSQL by CloudNativePG (it follows the Secret
#             data/pg-burnrate); pods keep their open connections and need a restart to open new ones
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

ROTATE=""; RESTART=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --restart) RESTART=1 ;;
    --rotate) ROTATE=${2:-}; shift || true
      case "$ROTATE" in jwt|internal|admin|db|all) ;; *) die "--rotate needs one of: jwt internal admin db all" ;; esac ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done

APP_ENV=$SECRETS_DIR/app.env
DOTENV=${ENV_FILE:-$REPO_DIR/.env}
PG_HOST=${PG_HOST:-pg-rw.data.svc.cluster.local}
FROM_ENV=(BR_OPENAI_API_KEY BR_ANTHROPIC_API_KEY BR_STRIPE_SECRET_KEY BR_STRIPE_WEBHOOK_SECRET
          BR_TWILIO_ACCOUNT_SID BR_TWILIO_AUTH_TOKEN BR_TWILIO_FROM BR_SMTP_URL)

# value of KEY from the environment, else from .env (optional surrounding quotes removed); empty when absent
lookup() {
  local key=$1 val=""
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
  elif [[ -f "$DOTENV" ]]; then
    val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$key=" "$DOTENV" | tail -1 | cut -d= -f2- || true)
    val=${val%$'\r'}
    case "$val" in
      \"*\") val=${val#\"}; val=${val%\"} ;;
      \'*\') val=${val#\'}; val=${val%\'} ;;
    esac
    printf '%s' "$val"
  fi
}
origin() { # where lookup() finds KEY: environment | .env | -
  local key=$1
  if [[ -n "${!key:-}" ]]; then echo "environment"
  elif [[ -f "$DOTENV" ]] && grep -qE "^[[:space:]]*(export[[:space:]]+)?$key=.+" "$DOTENV"; then echo ".env"
  else echo "-"; fi
}
urlencode() { jq -rn --arg v "$1" '$v|@uri'; }

# replace_env <file> <KEY> <value>: set KEY in a KEY=value file, keeping the other lines
replace_env() {
  local f=$1 k=$2 v=$3 tmp
  tmp=$(mktemp "$SECRETS_DIR/.tmp.XXXXXX")
  grep -vE "^$k=" "$f" > "$tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$f"
}

check_profile
need openssl jq

# ---- dry run: names and origins only ---------------------------------------------------------------------------
if dry; then
  log "DRY RUN: nothing is generated, nothing is applied"
  for k in BR_JWT_SECRET BR_INTERNAL_TOKEN BR_ADMIN_TOKEN; do
    if [[ -f "$APP_ENV" ]] && grep -qE "^$k=.+" "$APP_ENV"; then info "$k  <- deploy/secrets/app.env (kept)"
    else info "$k  <- would be generated into deploy/secrets/app.env"; fi
  done
  if [[ -f "$PG_ENV" ]]; then info "BR_DATABASE_URL  <- deploy/secrets/pg.env, host $PG_HOST, database {service}"
  else info "BR_DATABASE_URL  <- deploy/secrets/pg.env is missing: run bootstrap.sh (step postgres) first"; fi
  for k in "${FROM_ENV[@]}"; do
    o=$(origin "$k")
    if [[ "$o" != "-" ]]; then info "$k  <- $o"
    elif [[ "$k" == "BR_OPENAI_API_KEY" ]]; then info "$k  <- NOT SET (required while BR_LLM_PROVIDER=openai)"
    else info "$k  <- not set, left out"; fi
  done
  [[ -n "$ROTATE" ]] && info "would rotate: $ROTATE"
  info "would apply Secret $APP_NS/burnrate-secrets$( [[ $RESTART -eq 1 ]] && echo ' and restart every Deployment' )"
  exit 0
fi

need kubectl
require_context
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
umask 077

# ---- generated values ----------------------------------------------------------------------------------------------
ensure_env_file "$APP_ENV" BR_JWT_SECRET=@hex64 BR_INTERNAL_TOKEN=@rand BR_ADMIN_TOKEN=@rand
[[ -f "$PG_ENV" ]] || die "deploy/secrets/pg.env not found: run bootstrap.sh (step postgres) first, it creates the database password"

case "$ROTATE" in
  jwt|all)      replace_env "$APP_ENV" BR_JWT_SECRET "$(rand 32)";     info "rotated BR_JWT_SECRET" ;;
esac
case "$ROTATE" in
  internal|all) replace_env "$APP_ENV" BR_INTERNAL_TOKEN "$(rand 20)"; info "rotated BR_INTERNAL_TOKEN" ;;
esac
case "$ROTATE" in
  admin|all)    replace_env "$APP_ENV" BR_ADMIN_TOKEN "$(rand 20)";    info "rotated BR_ADMIN_TOKEN" ;;
esac
case "$ROTATE" in
  db|all)
    replace_env "$PG_ENV" PG_PASSWORD "$(rand 32)"
    apply_pg_secret
    info "rotated the PostgreSQL password; CloudNativePG applies it to role burnrate within a minute"
    ;;
esac

# ---- assemble KEY=value lines in a private temporary file ------------------------------------------------------------
TMP=$(mktemp -d); chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
OUT=$TMP/secret.env
: > "$OUT"
for k in BR_JWT_SECRET BR_INTERNAL_TOKEN BR_ADMIN_TOKEN; do
  v=$(env_get "$APP_ENV" "$k")
  [[ -n "$v" ]] || die "$k is empty in deploy/secrets/app.env (delete the file to generate new values)"
  printf '%s=%s\n' "$k" "$v" >> "$OUT"
done
PGP=$(env_get "$PG_ENV" PG_PASSWORD)
[[ -n "$PGP" ]] || die "PG_PASSWORD is empty in deploy/secrets/pg.env"
# {service} must reach the application literally: it puts the service name there
printf 'BR_DATABASE_URL=postgresql+asyncpg://burnrate:%s@%s:5432/{service}\n' "$(urlencode "$PGP")" "$PG_HOST" >> "$OUT"
unset PGP v

missing_openai=0
for k in "${FROM_ENV[@]}"; do
  v=$(lookup "$k")
  if [[ -n "$v" ]]; then
    case "$v" in *$'\n'*) die "$k contains a line break" ;; esac
    printf '%s=%s\n' "$k" "$v" >> "$OUT"
  elif [[ "$k" == "BR_OPENAI_API_KEY" ]]; then
    missing_openai=1
  fi
  unset v
done
if [[ $missing_openai -eq 1 ]]; then
  warn "BR_OPENAI_API_KEY is not set (export it, or put it in $DOTENV): statement reading and the rehearsal chat"
  warn "will not reach a model; the rest of the product works. Re-run this script once the key is there."
fi

# ---- apply ----------------------------------------------------------------------------------------------------------------
kubectl get ns "$APP_NS" >/dev/null 2>&1 || die "namespace $APP_NS does not exist: run bootstrap.sh first"
secret_from_env_file "$APP_NS" burnrate-secrets "$OUT"
info "keys: $(cut -d= -f1 "$OUT" | tr '\n' ' ')"

if [[ $RESTART -eq 1 ]]; then
  if [[ -n "$(kubectl -n "$APP_NS" get deploy -o name 2>/dev/null)" ]]; then
    kubectl -n "$APP_NS" rollout restart deployment >/dev/null
    info "every Deployment in $APP_NS is restarting: kubectl -n $APP_NS get pods -w"
  fi
else
  info "running pods keep the old values until they restart (--restart, or the next deploy.sh)"
fi
