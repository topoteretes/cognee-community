"""Discord connector demo: turn a Discord server's conversations into memory.

``discord_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Messages are grouped into one document per channel or thread per day and
ingested as normal documents, so they go through cognify entity extraction.

Re-running syncs only what changed: new messages update today's document,
recent edits and deletions are picked up, and deleted threads and channels are
forgotten.

Privacy / opt-in
----------------
This reads every message the bot can see. Nothing is fetched until you run this
script. Give the bot a role limited to the channels you want in memory and keep
Discord in its own dataset.

One-time setup
--------------
1. Create a bot in the Discord Developer Portal, turn on Message Content Intent,
   and invite it with View Channels and Read Message History (see the README).
2. Export the bot token, the server id and your LLM key, then run:

       export DISCORD_BOT_TOKEN="..."
       export DISCORD_GUILD_ID="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_discord import discord_source

DATASET_NAME = "discord"


async def main() -> None:
    required = ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    # Narrow the sync with channel_ids=[...] or since_days=... on a big server.
    source = discord_source()

    print("Syncing Discord messages into cognee ...")
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
        "What were the main decisions discussed on this server?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nPost, edit or delete a message, then re-run: only the changes are synced "
        "and deleted threads and channels are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
