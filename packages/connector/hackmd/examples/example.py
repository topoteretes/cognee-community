"""HackMD connector demo: turn HackMD notes into memory.

``hackmd_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every note is ingested as a normal document with its workspace, folder and tags,
so it goes through cognify entity extraction.

Re-running syncs only what changed: edited notes are processed again and deleted
ones are forgotten. Each run lists every selected workspace once, and HackMD's
free plan allows 400 API calls a month per workspace.

Privacy / opt-in
----------------
This reads every note the token can see in the selected workspaces, private notes
included. Nothing is fetched until you run this script. Keep HackMD in its own
dataset.

One-time setup
--------------
Create an API token under Settings > API in HackMD, then export it, optionally a
team workspace path (the part after ``/team/`` in its URL), and your LLM key, and
run:

    export HACKMD_API_TOKEN="..."
    export HACKMD_TEAM_PATH="my-team"   # optional
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_hackmd import hackmd_source

DATASET_NAME = "hackmd"


async def main() -> None:
    if not os.environ.get("HACKMD_API_TOKEN"):
        print("Set HACKMD_API_TOKEN to run this example.")
        return

    team = os.environ.get("HACKMD_TEAM_PATH")
    source = hackmd_source(
        token=os.environ["HACKMD_API_TOKEN"],
        team_paths=[team] if team else None,
        # Required with personal notes: it names this sync's state and cleanup.
        resource_name="hackmd_example",
    )

    print("Syncing HackMD into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "What did we decide in the latest meeting notes?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nEdit or delete a note, then re-run: only the changes are synced and deleted "
        "notes are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
