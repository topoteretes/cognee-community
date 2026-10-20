"""Help Scout connector demo - turn your support inbox and help center into memory.

Pull Help Scout conversations (with their threads) and, optionally, Docs articles
into cognee, incrementally and with forget-on-delete.

First run: backfills everything. Later runs: only conversations changed since the
last run are fetched. Deleted conversations and deleted or unpublished articles
are forgotten.

Privacy: this reads customer conversations. Nothing is fetched until you run it.

Setup:

    cd packages/connector/help-scout && uv sync --all-extras
    # Help Scout: Your Profile -> My Apps -> Create My App (App ID + App Secret)
    export HELPSCOUT_APP_ID="..."
    export HELPSCOUT_APP_SECRET="..."
    export HELPSCOUT_DOCS_API_KEY="..."   # optional: also sync Docs articles
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_help_scout import help_scout_source

DATASET_NAME = "help_scout"


async def main() -> None:
    app_id = os.environ.get("HELPSCOUT_APP_ID")
    app_secret = os.environ.get("HELPSCOUT_APP_SECRET")
    if not (app_id and app_secret):
        print("Set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET to run this example.")
        return

    source = help_scout_source(
        resource_name="help_scout",  # one per Help Scout account; keep it fixed
        app_id=app_id,
        app_secret=app_secret,
        docs_api_key=os.environ.get("HELPSCOUT_DOCS_API_KEY"),
        # mailbox_ids=[123], include_notes=True, reconcile_every=6, ...
    )

    print("Syncing Help Scout into cognee ...")
    # primary_key="id" + write_disposition="merge" are REQUIRED: incremental runs
    # only see changes, and tombstones remove deleted conversations by id.
    await cognee.remember(
        source, dataset_name=DATASET_NAME, primary_key="id", write_disposition="merge"
    )
    print("Run stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What do customers ask about most, and how did we answer?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
