#!/usr/bin/env bash
# Regenerates the per-service manifests from the code. Adding a service is one line in
# burnrate/services/__init__.py, then this script.
#
#   deploy/scripts/gen-apps.sh              # write the files, print what was found
#   deploy/scripts/gen-apps.sh --check      # write nothing; exit 1 when the files on disk are out of date (CI)
#   deploy/scripts/gen-apps.sh --quiet      # print only warnings
#   deploy/scripts/gen-apps.sh --help
#
# Reads   burnrate/services/__init__.py   SERVICES, WORKERS, LOOPS, JOBS
#         burnrate/**/*.py                @subscribe("topic", worker="..."), emit(s, SVC, "topic"), make_base("...")
#         deploy/apps/topics.txt          declared Kafka topics
#         deploy/apps/worker-topics.txt   optional manual worker -> topics lines
# Writes  deploy/apps/small/  deploy/apps/full/  deploy/apps/jobs/schema.yaml  deploy/apps/GENERATED.txt
#         deploy/platform/cnpg/databases.yaml  deploy/platform/strimzi/topics.yaml  deploy/compose/initdb.sh
#
# Sizes (replicas, requests, autoscaling limits per profile) are the tables at the top of gen_apps.py.
# REGISTRY / REGISTRY_NAME change the image prefix and the name of the pull secret.
# Needs python3 (3.9+, standard library only). The code is parsed, not imported.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --check|--quiet) ;;
    *) usage >&2; die "unknown argument: $arg" ;;
  esac
done
need python3
# -B: do not leave a __pycache__ directory next to the scripts
exec python3 -B "$SCRIPT_DIR/gen_apps.py" --registry "$REGISTRY" --pull-secret "$REGISTRY_NAME" "$@"
