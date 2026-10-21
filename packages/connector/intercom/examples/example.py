"""Intercom connector demo — turn your support history into memory.

Sync Intercom conversations (with replies and internal notes) and Help Center
articles into cognee, then ask about them. Re-run after replying to, deleting or
unpublishing something to see the re-sync and forget-on-delete.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads customer conversations. Nothing is fetched until you run this
script. Author names are kept; email addresses are not. Use a dedicated dataset
so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. In the Intercom Developer Hub, create an app on your workspace (a free
   development workspace works) and copy its access token.
2. Export it and your LLM key, then run:

       export INTERCOM_ACCESS_TOKEN=...
       export LLM_API_KEY=sk-...
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_intercom import intercom_source

# Keep Intercom in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "intercom"


async def main() -> None:
    if not os.environ.get("INTERCOM_ACCESS_TOKEN"):
        print("Set INTERCOM_ACCESS_TOKEN to run this example.")
        return

    base_url = os.environ.get("INTERCOM_BASE_URL", "https://api.intercom.io")

    print("Syncing Intercom into cognee ...")
    await cognee.remember(
        intercom_source(base_url=base_url),
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        self_improvement=False,
    )

    answer = await cognee.search(
        query_text="What problems do customers report most, and how were they resolved?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
