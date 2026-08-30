"""Telegram connector demo — turn your group and channel chatter into memory.

Sync messages your bot sees into cognee, incrementally via the Bot API's
``update_id`` offset. ``telegram_source`` returns a ``dlt`` resource you hand
straight to ``cognee.remember``. Messages are ingested as normal documents (so
they go through the full cognify entity-extraction pipeline), with sender /
chat / reply / forward context as metadata and folded into the text so the
relationships become graph edges.

Re-running is cheap: each run fetches only updates newer than the last
committed offset. Edits re-sync under the same id; removing the bot from a
chat forgets that chat's messages from memory on the next sync. (Per-message
deletions are invisible to the Bot API — see the README's honest-limits
section.)

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create a bot with @BotFather and export the token:

       export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."

2. Add the bot to the groups/channels you want to ingest (admin for channels)
   and disable group privacy mode in @BotFather (/setprivacy → Disable), then
   re-add the bot to existing groups. Bots only see messages sent after they
   join — send a few test messages.

3. Install and run:

       cd packages/connector/telegram && uv sync
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after new messages or edits to see the incremental re-sync; remove the
bot from a chat and re-run to see forget-on-removal.
"""

import asyncio
import os

import cognee

from cognee_community_connector_telegram import telegram_source

# Keep the chats in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "telegram"

# Restrict ingestion to specific chats (ids or @usernames); empty = all chats
# the bot receives updates for.  e.g. CHATS = ["@my_channel", -1001234567890]
CHATS: list[int | str] = []


async def main() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Set TELEGRAM_BOT_TOKEN (from @BotFather) to run this example.")
        return

    source = telegram_source(chats=CHATS)

    print(f"Syncing Telegram updates into cognee (chats filter: {CHATS or 'all'}) ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # incremental upsert by chat/message id
        max_rows_per_table=0,  # 0 = no row cap (busy chats exceed the default 50)
    )

    answer = await cognee.search(
        query_text="Summarize what these chats are discussing and who talks to whom.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nSend or edit a message in a connected chat, then re-run: only new "
        "updates sync. Remove the bot from a chat and re-run to see its "
        "messages reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
