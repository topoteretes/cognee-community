"""Ingest Caddyfile routing and reverse proxy rules into cognee memory via dlt.

Demonstrates:
1. Ingesting a ``Caddyfile`` containing site blocks, reverse proxies, and matchers.
2. Building an AI agent knowledge graph with ``cognee.cognify()``.
3. Asking complex questions about web server routing and upstream destinations with ``cognee.search()``.
4. Demonstrating forget-on-delete / orphan cleanup when a route is removed from Caddyfile.

Requires:
    pip install "cognee[dlt]"
    export LLM_API_KEY="your-llm-key"
"""

import asyncio
import os

import cognee
from cognee_community_connector_caddy import caddy_source

DATASET = "caddy_web_routing"
CURRENT_DIR = os.path.dirname(__file__)
CADDYFILE_PATH = os.path.join(CURRENT_DIR, "test_data", "Caddyfile")

QUESTION = "What backend port does /api/* requests proxy to on app.company.com?"


async def main() -> None:
    print("1. Initializing clean memory state...")
    await cognee.forget(everything=True)

    print("2. Ingesting Caddyfile routing configuration snapshot...")
    await cognee.remember(
        caddy_source(CADDYFILE_PATH),
        dataset_name=DATASET,
        max_rows_per_table=0,  # Ingest entire Caddyfile snapshot
    )

    print("3. Building knowledge graph from web server routing rules...")
    await cognee.cognify()

    print(f"\n4. Querying agent memory: '{QUESTION}'")
    answer = await cognee.search(QUESTION)
    print("\nAgent Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
