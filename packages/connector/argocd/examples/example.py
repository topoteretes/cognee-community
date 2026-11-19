"""Sync Argo CD applications into cognee memory and ask a question about them.

Every application and every sync-history entry becomes a document, which cognee chunks and
runs through LLM entity extraction.

Setup (see the README for details):
  1. Create a read-only account token in Argo CD:
       argocd account generate-token --account cognee --expires-in 90d
  2. If Argo CD is cluster-internal, forward its port in another terminal:
       kubectl port-forward svc/argocd-server -n argocd 8080:443
  3. Set:
       ARGOCD_BASE_URL=https://localhost:8080
       ARGOCD_AUTH_TOKEN=<the token>
       ARGOCD_INSTANCE_NAME=demo            (required for localhost URLs)
       ARGOCD_PROJECTS=default              (optional, comma-separated)
     plus the usual cognee LLM_API_KEY.
     A self-signed certificate needs a CA file (ca_bundle=...) or, for a throwaway local
     test only, verify_ssl=False.

Run:
    python examples/example.py ["optional question about your applications"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_argocd import argocd_source

DATASET = "argocd_demo"


async def main():
    missing = [name for name in ("ARGOCD_BASE_URL", "ARGOCD_AUTH_TOKEN") if not os.getenv(name)]
    if missing:
        print(f"Set {' and '.join(missing)} first. See the setup notes at the top of this file.")
        return

    # Reads ARGOCD_* env vars. Set verify_ssl=False or ca_bundle="..." for a self-signed cert.
    source = argocd_source()

    # Run this script again to see incremental sync: only changed applications and new sync
    # history are processed. Delete an application in Argo CD and run it once more: its
    # documents are forgotten. (Deleting most of them needs allow_mass_delete=True.)
    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" matches how the resource is declared; remember() would otherwise
        # default to "replace" and override it.
        write_disposition="merge",
        # No row cap: the default stops after 50 rows.
        max_rows_per_table=0,
    )
    # Counts only, never application names or content.
    print("Sync stats:", source.cognee_sync_stats)

    question = sys.argv[1] if len(sys.argv) > 1 else "Which applications are not healthy, and why?"
    results = await cognee.search(question, datasets=[DATASET])
    print("Search:", results)


if __name__ == "__main__":
    asyncio.run(main())
