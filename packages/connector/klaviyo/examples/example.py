"""Klaviyo connector demo: turn an account's campaigns, flows and email templates into memory.

Pulls campaigns (each message's subject, preview text and copy), flows (the
trigger and every step) and email templates into cognee, incrementally and with
forget-on-delete. ``klaviyo_source`` returns a ``dlt`` resource you hand to
``cognee.remember``. Rows are ingested as normal documents, so they go through
the full cognify entity-extraction pipeline.

Run it twice. The first run ingests the account. Before the second run, edit a
draft campaign's subject, change a template, or archive a campaign: only the
changes are synced, and the archived campaign is forgotten.

Privacy / opt-in
----------------
Nothing is fetched until you run this script. Profiles, lists and sender
addresses are never read or stored (see the README). Use a dedicated dataset so
you can wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-klaviyo

2. In Klaviyo, open Settings > Account > API keys and create a private key with
   read access to Accounts, Campaigns, Flows and Templates (nothing else).
3. Export the key and your LLM key, then run:

       export KLAVIYO_API_KEY="pk_..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_klaviyo import klaviyo_source

# Keep the account in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "klaviyo"


async def main() -> None:
    if not os.environ.get("KLAVIYO_API_KEY"):
        print("Set KLAVIYO_API_KEY to run this.")
        return

    print("Syncing Klaviyo into cognee ...")
    source = klaviyo_source()
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every record that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )
    # listed, rendered, unchanged, deleted, kept, failed
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.recall(
        "What discount do our campaigns offer, and what does a new subscriber receive?",
        datasets=[DATASET_NAME],
    )
    print("\nRecall result:\n", answer)

    print(
        "\nEdit a draft campaign or a template, or archive a campaign, then re-run: only the "
        "changes are synced and archived or deleted records are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
