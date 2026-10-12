#!/usr/bin/env bash
# ============================================================================
# run_local.sh - Run collection and verification locally
#
#   bash scripts/run_local.sh                     # Collect one chunk, build and verify
#   bash scripts/run_local.sh --no-crawl          # Rebuild using existing CSV files
#   bash scripts/run_local.sh --crawl-until-done  # Collect all chunks, then build
#   bash scripts/run_local.sh --status            # Show collection progress
#   bash scripts/run_local.sh --new-campaign      # Start a new campaign and reset page cursors
#
# Environment variables:
#   CRAWL_BUDGET_SECONDS   Chunk budget in seconds; default 4200
#   CRAWL_MAX_PAGES        Maximum pages this chunk; 0 means unlimited
#
# Create a virtual environment before first use:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

DO_CRAWL=1
UNTIL_DONE=0
NEW_CAMPAIGN=0
STATUS_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --no-crawl)         DO_CRAWL=0 ;;
    --crawl-until-done) UNTIL_DONE=1 ;;
    --new-campaign)     NEW_CAMPAIGN=1 ;;
    --status)           STATUS_ONLY=1 ;;
    -h|--help)          sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# Prefer the local virtual environment, then python3.
if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
  PY="${ROOT_DIR}/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

step "Python interpreter: ${PY}  ($(${PY} -V 2>&1))"

if [ "${STATUS_ONLY}" = "1" ]; then
  "${PY}" standard_crawl.py --status
  exit 0
fi

step "Check dependencies"
"${PY}" - <<'EOF'
import importlib.util
import sys

missing = [m for m in ("pandas", "requests", "bs4") if importlib.util.find_spec(m) is None]
if missing:
    sys.exit("Missing dependencies: %s\nRun: %s -m pip install -r requirements.txt" % (", ".join(missing), sys.executable))
print("Dependencies OK")
EOF

# ---------------------------------------------------------------- Collection
CRAWL_ARGS=(--budget-seconds "${CRAWL_BUDGET_SECONDS:-4200}")
[ "${NEW_CAMPAIGN}" = "1" ] && CRAWL_ARGS+=(--new-campaign)
[ -n "${CRAWL_MAX_PAGES:-}" ] && CRAWL_ARGS+=(--max-pages "${CRAWL_MAX_PAGES}")

if [ "${DO_CRAWL}" = "1" ]; then
  if [ "${UNTIL_DONE}" = "1" ]; then
    step "Collect chunks until complete (Ctrl-C to stop; progress is saved)"
    round=0
    while :; do
      round=$((round + 1))
      step "Collection chunk #${round}"
      if ! "${PY}" standard_crawl.py "${CRAWL_ARGS[@]}"; then
        echo "Collection chunk failed; stopped." >&2
        exit 1
      fi
      NEXT="$("${PY}" standard_crawl.py --emit-next | sed -E 's/.*crawl_next=([^]]*).*/\1/')"
      if [ "${NEXT}" = "build" ]; then
        break
      elif [ "${NEXT}" != "crawl" ]; then
        echo "Collection cannot continue (next=${NEXT})." >&2
        exit 1
      fi
      CRAWL_ARGS=(--budget-seconds "${CRAWL_BUDGET_SECONDS:-4200}")   # Do not reset cursors on continuation.
    done
  else
    step "Collect one resumable chunk (use --crawl-until-done for a full run)"
    "${PY}" standard_crawl.py "${CRAWL_ARGS[@]}"
  fi
else
  step "Skip collection (--no-crawl)"
fi

"${PY}" standard_crawl.py --status

# ---------------------------------------------------------------- Build
step "Build standard_db from collected CSV files"
"${PY}" build_db.py

step "Verify standard_db"
"${PY}" build_db.py --verify

step "Done"
