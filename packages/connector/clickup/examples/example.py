"""ClickUp connector demo — turn your ClickUp workspace into AI memory.

Pull ClickUp tasks (including subtasks, checklists, custom fields, and comments) and
Docs into cognee, with incremental re-sync and forget-on-delete. ``clickup_source``
returns a ``dlt`` source you pass directly to ``cognee.remember``.

Each task is rendered into rich Markdown and ingested via Cognee's document-mode
(so each task flows through normal cognify LLM entity extraction and knowledge
graph construction).

Subsequent runs use ClickUp's ``date_updated`` (Unix ms cursor) to pull only
modified tasks, while deleted tasks are pruned from memory via orphan cleanup.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Generate a Personal API token in ClickUp:
   Click your avatar -> Settings -> Apps -> API Token -> "Generate".
   Copy the generated token (starts with "pk_...").

2. Export the token and your LLM key:
   export CLICKUP_API_TOKEN="pk_..."
   export LLM_API_KEY="sk-..."

3. Run this script:
   uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_clickup import clickup_source

DATASET_NAME = "clickup_workspace"


async def main() -> None:
    api_token = os.environ.get("CLICKUP_API_TOKEN")
    if not api_token:
        print("Error: Set CLICKUP_API_TOKEN to run this example.")
        print("Generate one in ClickUp: avatar -> Settings -> Apps -> API Token")
        return

    # 1. Initialize the ClickUp DLT source
    # Optionally pass team_id (required if the token sees several Workspaces) and
    # space_ids / folder_ids / list_ids to limit what is ingested.
    source = clickup_source(
        api_token=api_token,
        include_comments=True,
        include_closed=True,
        include_docs=True,
    )

    print(f"Syncing ClickUp workspace into dataset '{DATASET_NAME}'...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print("Ingestion & cognification complete!\n")

    # 2. Query the generated knowledge graph and vector store
    queries = [
        "What tasks are currently in progress, and who is assigned to them?",
        "What blockers or discussions were mentioned in task comments?",
        "What do our ClickUp Docs say about the architecture?",
    ]

    for q in queries:
        print(f"--- Query: {q} ---")
        results = await cognee.search(
            query_text=q,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"Answer:\n{results}\n")


if __name__ == "__main__":
    asyncio.run(main())
