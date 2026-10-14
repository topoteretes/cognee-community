"""Telegram connector demo: "ask my group chats".

Syncs the messages a bot can see in your selected chats into cognee memory,
then asks a question about them. Run it again later: only new updates are
fetched, edits replace the old text, and if you remove the bot from a chat
every message from that chat is forgotten.

One-time setup
--------------
1. In Telegram, talk to @BotFather: ``/newbot``, then copy the token.
2. For groups, either disable privacy mode (@BotFather -> /setprivacy ->
   Disable) or make the bot an admin, otherwise it only sees commands and
   replies to itself. For channels, add the bot as an admin.
3. Add the bot to the chats, then send a few messages. A bot only sees
   messages sent after it joined.
4. Find chat ids: run this script once with ``TELEGRAM_CHATS`` unset; the log
   line "chats seen in this run" lists each chat with its id. Then set e.g.
   ``TELEGRAM_CHATS=-1001234567890,@my_channel``.
5. Set ``TELEGRAM_BOT_TOKEN`` and your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_telegram import telegram_source

DATASET_NAME = "telegram_chats"

# write_disposition="merge" is REQUIRED: getUpdates only returns new updates, so
#   the default "replace" would wipe earlier messages on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
TELEGRAM_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


def _chats_from_env():
    raw = os.environ.get("TELEGRAM_CHATS", "").strip()
    if not raw:
        return None
    return [c.strip() for c in raw.split(",") if c.strip()]


async def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Set TELEGRAM_BOT_TOKEN first (see the setup steps at the top of this file).")
        return

    chats = _chats_from_env()
    print(f"Syncing {'all chats the bot is in' if chats is None else chats} ...")
    result = await cognee.remember(
        telegram_source(chats=chats),
        dataset_name=DATASET_NAME,
        **TELEGRAM_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "What has been discussed in these chats, and who said what?",
            datasets=[DATASET_NAME],
        )
    except Exception as error:  # cognee raises NoDataError while the dataset is empty
        if type(error).__name__ != "NoDataError":
            raise
        print(
            "Nothing to search yet: the bot has not seen any messages. Send a few "
            "messages in a chat the bot is in, then run this again."
        )
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
