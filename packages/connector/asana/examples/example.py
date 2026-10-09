"""Asana connector demo: turn your Asana projects into memory.

Pulls tasks (with comments and subtask titles) and project descriptions into
cognee, incrementally and with forget-on-delete. ``asana_source`` returns a
``dlt`` source you hand to ``cognee.remember``. Rows are ingested as normal
documents, so they go through the full cognify entity-extraction pipeline.

Run it twice. The first run ingests everything in the selected projects. Before
the second run, edit a task, edit a comment or delete a task in Asana: only the
changed tasks are fetched again, and the deleted one is forgotten.

Privacy / opt-in
----------------
This reads the content of your Asana tasks. Nothing is fetched until you run
this script. Scope what you ingest with ``project_gids``, and use a dedicated
dataset so you can wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-asana

2. Create a personal access token at https://app.asana.com/0/my-apps.
3. Export the token, the projects to ingest and your LLM key, then run:

       export ASANA_ACCESS_TOKEN="..."
       export ASANA_PROJECT_GIDS="1201234567890123,1209876543210987"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   A project's gid is the number in its URL: https://app.asana.com/0/<gid>/...
"""

import asyncio
import os

import cognee

from cognee_community_connector_asana import asana_source

# Keep Asana in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "asana"


async def main() -> None:
    raw_gids = os.environ.get("ASANA_PROJECT_GIDS", "")
    project_gids = [g.strip() for g in raw_gids.split(",") if g.strip()]
    if not os.environ.get("ASANA_ACCESS_TOKEN") or not project_gids:
        print("Set ASANA_ACCESS_TOKEN and ASANA_PROJECT_GIDS (comma-separated) to run this.")
        return

    print(f"Syncing {len(project_gids)} Asana project(s) into cognee ...")
    await cognee.remember(
        asana_source(project_gids=project_gids),
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every task that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What are these Asana projects about, and what is still open?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a task, edit a comment or delete a task in Asana, then re-run: only the "
        "changes are synced and deleted tasks are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
