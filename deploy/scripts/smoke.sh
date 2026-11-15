#!/usr/bin/env bash
# Checks the public address of a deployed Burn Rate from the outside. Read-only: only GET requests.
#
#   deploy/scripts/smoke.sh                                   # host from deploy/.state/host (written by deploy.sh)
#   deploy/scripts/smoke.sh burnrate.203-0-113-10.sslip.io    # or name it
#   deploy/scripts/smoke.sh --help
#
# Checks (exit code 1 if any fails)
#   1. landing page          GET https://HOST/                       200, HTML
#   2. API                   GET https://HOST/api/health             200
#   3. internal API closed   GET https://HOST/internal/engine/rules  404 (never 200, never a redirect)
#   4. same, by a detour     GET https://HOST/api/../internal/engine/rules (sent as written)   not 200
#   5. metrics closed        GET https://HOST/metrics                404, and no Prometheus text in the body
#   6. HTTP goes to HTTPS    GET http://HOST/                        301 or 308 with Location: https://HOST/...
#   7. certificate           trusted chain for HOST (curl verifies it), not a Let's Encrypt STAGING one,
#                            more than 14 days left
#
# Checks 3-5 are the "nothing under /internal or /metrics is reachable from outside" rule of
# deploy/apps/common/routes.yaml.
# Environment: SMOKE_TIMEOUT (seconds per request, default 15), SMOKE_RETRIES (default 3, for a load balancer
# that is still warming up).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

TARGET=""
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "unknown option: $arg" ;;
    *) TARGET=$arg ;;
  esac
done
need curl openssl
[[ -n "$TARGET" ]] || TARGET=${HOST:-$(state_get host)}
[[ -n "$TARGET" ]] || die "which host? pass it as an argument (deploy.sh records it in deploy/.state/host)"
TARGET=${TARGET#https://}; TARGET=${TARGET#http://}; TARGET=${TARGET%%/*}
T=${SMOKE_TIMEOUT:-15}
RETRIES=${SMOKE_RETRIES:-3}

pass=0; fail=0
ok()  { printf '  \033[32mok\033[0m    %s\n' "$*"; pass=$(( pass + 1 )); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$(( fail + 1 )); }

BODY=$(mktemp); HEAD=$(mktemp); trap 'rm -f "$BODY" "$HEAD"' EXIT

# fetch <url> [curl options...] -> sets CODE (000 when the connection failed), body in $BODY, headers in $HEAD
fetch() {
  local url=$1; shift
  CODE=000
  for _ in $(seq 1 "$RETRIES"); do
    CODE=$(curl -sS -o "$BODY" -D "$HEAD" -w '%{http_code}' --max-time "$T" "$@" "$url" 2>/dev/null) || CODE=000
    [[ "$CODE" != "000" && "$CODE" != "502" && "$CODE" != "503" ]] && return 0
    sleep 2
  done
  return 0
}
header() { tr -d '\r' < "$HEAD" | awk -v h="$1" 'BEGIN{h=tolower(h)":"} tolower($1)==h {sub(/^[^:]*: */,""); v=$0} END{print v}'; }

log "smoke test of https://$TARGET"

# 1. landing page
fetch "https://$TARGET/"
ctype=$(header content-type)
if [[ "$CODE" == "200" && "$ctype" == text/html* ]]; then ok "landing page: 200, $ctype"
else bad "landing page: got $CODE ${ctype:+($ctype)}, expected 200 text/html"; fi

# 2. API
fetch "https://$TARGET/api/health"
if [[ "$CODE" == "200" ]]; then ok "/api/health: 200 $(head -c 120 "$BODY" | tr -d '\n')"
else bad "/api/health: got $CODE, expected 200"; fi

# 3. internal API must not be reachable
fetch "https://$TARGET/internal/engine/rules"
if [[ "$CODE" == "404" ]]; then ok "/internal/engine/rules: 404 (not reachable from outside)"
else bad "/internal/engine/rules: got $CODE, expected 404"; fi

# 4. the same through a path the proxy has to normalise first
fetch "https://$TARGET/api/../internal/engine/rules" --path-as-is
case "$CODE" in
  200|301|302|307|308) bad "/api/../internal/engine/rules (sent as written): got $CODE" ;;
  000) bad "/api/../internal/engine/rules: no answer" ;;
  *) ok "/api/../internal/engine/rules (sent as written): $CODE" ;;
esac

# 5. metrics must not be reachable
fetch "https://$TARGET/metrics"
if grep -q -E '^# (HELP|TYPE) ' "$BODY" 2>/dev/null; then bad "/metrics: Prometheus metrics are PUBLIC (status $CODE)"
elif [[ "$CODE" == "404" ]]; then ok "/metrics: 404 (not reachable from outside)"
else bad "/metrics: got $CODE, expected 404"; fi

# 6. HTTP redirects to HTTPS
fetch "http://$TARGET/"
loc=$(header location)
if [[ ( "$CODE" == "301" || "$CODE" == "308" ) && "$loc" == "https://$TARGET"* ]]; then ok "http:// redirects to https:// ($CODE -> $loc)"
else bad "http://$TARGET/: got $CODE ${loc:+Location: $loc}, expected 301 or 308 to https://$TARGET/"; fi

# 7. certificate. Checks 1-5 already needed a chain curl trusts for this host name; here: issuer and time left.
cert=$(echo | openssl s_client -servername "$TARGET" -connect "$TARGET:443" 2>/dev/null | openssl x509 2>/dev/null || true)
if [[ -z "$cert" ]]; then
  bad "certificate: could not read it from $TARGET:443"
else
  issuer=$(printf '%s\n' "$cert" | openssl x509 -noout -issuer | sed 's/^issuer= *//')
  enddate=$(printf '%s\n' "$cert" | openssl x509 -noout -enddate | cut -d= -f2)
  if ! curl -sS -o /dev/null --max-time "$T" "https://$TARGET/" 2>/dev/null; then
    bad "certificate: curl does not trust it for $TARGET (issuer: $issuer)"
  elif printf '%s' "$issuer" | grep -qi 'staging'; then
    bad "certificate: issued by Let's Encrypt STAGING ($issuer); browsers will warn"
  elif ! printf '%s\n' "$cert" | openssl x509 -noout -checkend $(( 14 * 24 * 3600 )) >/dev/null; then
    bad "certificate: expires within 14 days ($enddate)"
  else
    ok "certificate: trusted, issuer $issuer, valid until $enddate"
  fi
fi

echo
if (( fail > 0 )); then
  printf '\033[1;31m%d of %d checks failed\033[0m for https://%s\n' "$fail" $(( pass + fail )) "$TARGET"
  exit 1
fi
printf '\033[1;32mall %d checks passed\033[0m: https://%s\n' "$pass" "$TARGET"
