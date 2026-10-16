"""Help Scout connector demo - turn your support inbox and help center into memory.

Pull Help Scout conversations (with their threads) and, optionally, Docs articles
into cognee, incrementally and with forget-on-delete. ``help_scout_source``
returns a ``dlt`` source you hand to ``cognee.remember``.

First run: backfills everything. Later runs: only conversations changed since the
last run are fetched (``modifiedSince``). Deleted conversations and deleted or
unpublished articles are forgotten on the next run.

Privacy: this reads customer conversations. Nothing is fetched until you run it.
Limit it with ``mailbox_ids=[...]`` and use a dedicated dataset.

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
    if not (os.environ.get("HELPSCOUT_APP_ID") and os.environ.get("HELPSCOUT_APP_SECRET")):
        print("Set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET to run this example.")
        return

    # Limit with mailbox_ids=[...]; set include_notes=True to add internal notes.
    source = help_scout_source()

    print("Syncing Help Scout into cognee ...")
    # write_disposition="merge" is REQUIRED: incremental runs only see changes.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    answer = await cognee.search(
        query_text="What do customers ask about most, and how did we answer?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nReply to or delete a conversation in Help Scout, then re-run: only the "
        "changes are fetched and deleted conversations are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
