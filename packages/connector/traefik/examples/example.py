"""Traefik connector demo — "Ask my Reverse Proxy Infrastructure".

Sync live Traefik routers, backend services, and middlewares into Cognee memory,
incrementally, with automated forget-on-delete.

────────────────────────────────────────────────────────────────────────────
One-time setup:
────────────────────────────────────────────────────────────────────────────
1. Install the package:
       pip install cognee-community-connector-traefik

2. Point to your Traefik API instance (e.g. running locally on port 8080):
       export TRAEFIK_API_URL="http://localhost:8080"
       # If auth is required:
       # export TRAEFIK_API_TOKEN="your-token"
       # or export TRAEFIK_USERNAME="admin" && export TRAEFIK_PASSWORD="password"

3. Set your LLM keys in .env as required by cognee.

Run it:
    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_traefik import traefik_source

DATASET_NAME = "traefik_infrastructure"
RESOURCE_NAME = "traefik_cluster_main"

TRAEFIK_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_url: str) -> None:
    source = traefik_source(api_url=api_url, resource_name=RESOURCE_NAME)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **TRAEFIK_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", getattr(source, "cognee_sync_stats", {}))


async def main():
    api_url = os.environ.get("TRAEFIK_API_URL", "http://localhost:8080")
    print(f"\n=== Syncing Traefik infrastructure from {api_url} ===")
    await sync(api_url)

    print("\n=== Asking Cognee about routing topology ===")
    query = (
        "Which public HTTP routers do not have rate-limiting "
        "or authentication middlewares attached?"
    )
    answer = await cognee.search(
        query_text=query,
        dataset_name=DATASET_NAME,
    )
    print("\nAnswer from Cognee Knowledge Graph:")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
