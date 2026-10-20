"""ClickUp connector demo — turn your ClickUp workspace into searchable AI memory.

Pulls ClickUp tasks into cognee, with forget-on-delete. ``clickup_source`` returns
a ``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Tasks are ingested as normal documents (so they go through the full
cognify entity-extraction pipeline, unlike relational dlt connectors).

Each run is a full snapshot: unchanged tasks maintain stable IDs and are not
re-cognified, while tasks that are deleted or moved out of scope drop out of the
snapshot, so cognee's orphan cleanup forgets them from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Generate a ClickUp Personal API Token:
   Go to ClickUp -> Settings -> Apps -> Click 'Generate' under API Token.
2. Export the token and your LLM key:

       export CLICKUP_API_TOKEN="pk_..."
       export LLM_API_KEY="sk-..."
       python examples/example.py

Re-run after editing or completing tasks in ClickUp to observe re-syncing.
"""

import asyncio
import os

import cognee
from cognee_community_connector_clickup import clickup_source

# Keep ClickUp in its own dataset for easy inspection and lifecycle management
DATASET_NAME = "clickup"


async def main() -> None:
    if not os.environ.get("CLICKUP_API_TOKEN"):
        print("Set CLICKUP_API_TOKEN to run this example.")
        print("Example: export CLICKUP_API_TOKEN='pk_your_token_here'")
        return

    # Scope with space_ids=[...] or list_ids=[...]; omit to sync the entire workspace
    source = clickup_source()

    print("Syncing ClickUp tasks into cognee memory ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    print("\nQuerying knowledge graph ...")
    answer = await cognee.search(
        query_text="What are the highest priority tasks currently open, and who is assigned to them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
