"""Telegram connector demo — turn group/channel messages into memory.

Syncs messages the bot can see into cognee, incrementally: the first run
backfills, later runs fetch only new updates (the ``update_id`` cursor lives in
dlt state), and edited messages rewrite their row instead of duplicating it.
``telegram_source`` returns a ``dlt`` resource you hand straight to
``cognee.remember`` — no routing kwargs needed. Messages are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline,
unlike the relational dlt connectors).

Requires a bot token (see README for @BotFather setup) and ``LLM_API_KEY``
like any other cognee run.
"""

import asyncio
import os

import cognee

from cognee_community_connector_telegram import telegram_source

# Keep chats in their own dataset so they are easy to inspect and forget.
DATASET_NAME = "telegram"


async def main() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print(
            "Set TELEGRAM_BOT_TOKEN (create a bot via @BotFather and add it to "
            "a group/channel first) to run this example."
        )
        return
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    print("Syncing Telegram messages into cognee ...")
    await cognee.remember(telegram_source(limit=50), dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the recent discussion.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nSend or edit a message in a chat the bot can see, then re-run: only "
        "new updates sync, and edits rewrite their row."
    )


if __name__ == "__main__":
    asyncio.run(main())
