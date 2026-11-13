"""Mattermost connector demo: "ask my team chat".

Syncs the threads of a few channels into cognee memory, then asks a question about
them. Run it again later: only threads that changed are re-read, and deleted
threads (or channels dropped from MATTERMOST_CHANNELS) are forgotten.

One-time setup
--------------
1. Enable personal access tokens on the server (System Console → Integrations →
   Integration Management) and create one under Profile → Security.
2. Set ``MATTERMOST_URL`` (like ``https://chat.example.com``, or
   ``http://localhost:8065`` for the Docker preview) and ``MATTERMOST_TOKEN``.
3. Set ``MATTERMOST_CHANNELS`` to a comma-separated list of channel ids (click the
   channel's name to open its Info panel; the id is the "ID:" line).
4. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_mattermost import mattermost_source

DATASET_NAME = "mattermost_chat"

# write_disposition="merge" is REQUIRED: later runs only return the threads that
#   changed, so the default "replace" would wipe the others on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
MATTERMOST_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    url = os.environ.get("MATTERMOST_URL")
    token = os.environ.get("MATTERMOST_TOKEN")
    channels = [c.strip() for c in os.environ.get("MATTERMOST_CHANNELS", "").split(",")]
    channels = [c for c in channels if c]
    if not url or not token or not channels:
        print("Set MATTERMOST_URL, MATTERMOST_TOKEN and MATTERMOST_CHANNELS first.")
        return

    # The connector takes everything as arguments; only this example reads the environment.
    source = mattermost_source(url, token=token, channels=channels)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **MATTERMOST_REMEMBER_KWARGS)
    print(result)
    # Counts only, never message text.
    print("sync stats:", source.cognee_sync_stats)

    try:
        answers = await cognee.recall(
            "What did the team decide, and why?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print("Nothing to search yet: the selected channels have no messages.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
