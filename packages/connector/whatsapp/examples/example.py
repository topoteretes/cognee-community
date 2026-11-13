"""WhatsApp connector demo: turn exported chats into memory.

Reads WhatsApp "Export chat" files and turns each chat into one document per
day in cognee. ``whatsapp_source`` returns a ``dlt`` source you hand to
``cognee.remember``. Rows are ingested as normal documents, so they go through
the full cognify entity-extraction pipeline.

Run it twice. The first run ingests every day in the exports. Export the chat
again later and re-run: only new and changed days are processed, and a day
whose messages were all deleted for everyone is forgotten.

Privacy / opt-in
----------------
An export holds other people's messages, so only ingest chats whose members
are fine with it. Nothing is read until you run this script. Phone numbers of
senders who aren't in your contacts are masked (``+…1234``) by default. Use a
dedicated dataset so you can wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-whatsapp

2. In WhatsApp, open a chat, then More > Export chat (Android) or tap the chat
   name > Export Chat (iPhone). "Without media" keeps up to 4x more history.
3. Point the script at the exports (files or a folder) and set your LLM key:

       export WHATSAPP_EXPORTS="/path/to/WhatsApp Chat with Suppliers.txt"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Separate several paths with the OS path separator (":" on macOS/Linux, ";" on Windows).
"""

import asyncio
import os

import cognee

from cognee_community_connector_whatsapp import whatsapp_source

# Keep WhatsApp in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "whatsapp"


async def main() -> None:
    raw = os.environ.get("WHATSAPP_EXPORTS", "")
    paths = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    if not paths:
        print("Set WHATSAPP_EXPORTS to one or more export files or folders to run this.")
        return

    source = whatsapp_source(paths)
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every day that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )
    # files, failed_files, days_new, days_changed, days_unchanged, days_deleted, ...
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What was decided or promised in these chats, and by whom?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nExport the chat again later and re-run: only new and changed days are "
        "synced, and days emptied by deleted messages are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
