#!/usr/bin/env bash
#
# Wipe stale cognee namespaces from TurboPuffer, then rebuild the Alice graph
# by running the example end-to-end.
#
# Usage:
#   ./reset_and_run.sh            # delete cognee/test namespaces, then run example
#   ./reset_and_run.sh --all      # delete ALL namespaces in the region first
#
# Prereqs: <repo>/.env with LLM_API_KEY + TURBOPUFFER_API_KEY; <repo>/.venv set up.
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PKG_DIR/../../../.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; source "$REPO_ROOT/.env"; set +a
fi
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi

# Required so the example uses this adapter; region defaults to the community default.
export TURBOPUFFER_REGION="${TURBOPUFFER_REGION:-gcp-us-central1}"
export GRAPH_DATABASE_PROVIDER=turbopuffer
export ENABLE_BACKEND_ACCESS_CONTROL=false
# Make the Alice corpus discoverable even if the package isn't beside the repo data.
export ALICE_DATA_PATH="${ALICE_DATA_PATH:-$REPO_ROOT/notebooks/data/alice_in_wonderland.txt}"

cd "$PKG_DIR"

echo "==> 1/2 deleting old TurboPuffer namespaces (region=$TURBOPUFFER_REGION)"
python examples/reset_namespaces.py "$@"

echo
echo "==> 2/2 running example (add -> cognify -> search) on Alice in Wonderland"
python examples/example.py
