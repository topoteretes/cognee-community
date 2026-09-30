"""Lever connector demo — turn your job postings into memory.

``lever_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Postings are ingested as normal documents (so they go through the full cognify
entity-extraction pipeline). Re-running only re-reads what changed in Lever, and
postings deleted in Lever are forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Only job postings are read by default. Interview feedback and notes are
restricted candidate data: enable them explicitly with ``include_feedback=True``
/ ``include_notes=True`` (candidate contact details are never ingested), and
keep them in a dedicated dataset so you can wipe it with one call.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install:  cd packages/connector/lever && uv sync --all-extras
2. Create an API key in Lever: Settings → Integrations and API → API credentials.
3. Export the key and your LLM key, then run:

       export LEVER_API_KEY="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_lever import lever_source

DATASET_NAME = "lever"


async def main() -> None:
    if not os.environ.get("LEVER_API_KEY"):
        print("Set LEVER_API_KEY to run this example.")
        return

    source = lever_source(posting_states=["published", "internal"])

    print("Syncing Lever postings into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required for incremental sync
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Summarize the open roles and the skills they have in common.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a posting in Lever, then re-run: only the change is "
        "re-synced and deleted postings are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
