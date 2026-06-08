#!/usr/bin/env bash
#
# Run the TurboPuffer graph adapter tests.
#
# Usage:
#   ./run_tests.sh             # contract + live integration + isolation (no LLM)
#   ./run_tests.sh e2e         # also run the Alice in Wonderland end-to-end (uses LLM)
#   ./run_tests.sh contract    # registration/boundary tests only (no network)
#
# Prereqs:
#   - cognee repo .env contains LLM_API_KEY and TURBOPUFFER_API_KEY
#   - the venv at <repo>/.venv exists and has the package installed:
#       uv pip install -e cognee-community/packages/graph/turbopuffer
#
set -euo pipefail

# --- locate the repo root (../../../.. from this package) and load config -----
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PKG_DIR/../../../.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; source "$REPO_ROOT/.env"; set +a
else
  echo "WARNING: $REPO_ROOT/.env not found; relying on already-exported env vars." >&2
fi

# activate venv if present
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi

# TurboPuffer region: default to the community-package default if unset.
export TURBOPUFFER_REGION="${TURBOPUFFER_REGION:-gcp-us-central1}"

# rtk (the shell hook) collapses pytest output; prefer "rtk proxy" if available.
RUN="python -m pytest"
if command -v rtk >/dev/null 2>&1; then
  RUN="rtk proxy python -m pytest"
fi

cd "$PKG_DIR"

mode="${1:-integration}"
echo ">> mode=$mode  region=$TURBOPUFFER_REGION  key=${TURBOPUFFER_API_KEY:0:4}****"

case "$mode" in
  contract)
    $RUN tests/test_registration.py -q
    ;;
  integration)
    # Live per-method + isolation tests (TurboPuffer only, no LLM cost).
    COGNEE_TURBOPUFFER_GRAPH_TESTS=1 \
      $RUN tests/test_registration.py tests/test_adapter_methods.py tests/test_dataset_isolation.py -q
    ;;
  e2e)
    # Everything above PLUS the Alice in Wonderland full pipeline (uses LLM_API_KEY).
    # The pipeline runs in single-tenant mode; multi-tenant (access-control) mode
    # needs the dataset handler selected via graph_dataset_database_handler.
    ENABLE_BACKEND_ACCESS_CONTROL=false \
    COGNEE_TURBOPUFFER_GRAPH_TESTS=1 COGNEE_TURBOPUFFER_GRAPH_E2E=1 \
      $RUN tests/ -q -s
    ;;
  *)
    echo "Unknown mode: $mode  (use: contract | integration | e2e)" >&2
    exit 2
    ;;
esac
