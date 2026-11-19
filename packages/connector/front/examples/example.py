"""Front connector demo: "ask my support inbox".

Syncs the conversations of a few inboxes into cognee memory, then asks a question about
them. Run it again later: only conversations that changed are re-read, and trashed,
deleted, merged-away or moved-out conversations are forgotten.

One-time setup
--------------
1. Create a Front API token that can read the inboxes (Settings → Developers → API
   tokens) and set ``FRONT_TOKEN``.
2. Run this script without ``FRONT_INBOXES``: it lists the inboxes the token can see,
   with their ids. Set ``FRONT_INBOXES`` to a comma-separated list of those ids.
3. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_front import front_source
from cognee_community_connector_front.front import FrontClient

DATASET_NAME = "front_support"

# write_disposition="merge" is REQUIRED: later runs only return the conversations that
#   changed, so the default "replace" would wipe the others on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
FRONT_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


def list_inboxes(token: str) -> None:
    inboxes = FrontClient(token).get("/inboxes").get("_results", [])
    print("Inboxes this token can read (set FRONT_INBOXES to the ids you want):")
    for inbox in inboxes:
        print(f"   {inbox.get('id')}   {inbox.get('name')}")


async def main():
    token = os.environ.get("FRONT_TOKEN")
    inboxes = [i.strip() for i in os.environ.get("FRONT_INBOXES", "").split(",") if i.strip()]
    if not token:
        print("Set FRONT_TOKEN first.")
        return
    if not inboxes:
        list_inboxes(token)
        return

    # The connector takes everything as arguments; only this example reads the environment.
    source = front_source(token=token, inboxes=inboxes)
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **FRONT_REMEMBER_KWARGS)
    print(result)
    # Counts only, never message text.
    print("sync stats:", source.cognee_sync_stats)

    try:
        answers = await cognee.recall(
            "What problems did customers report, and how did we resolve them?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print("Nothing to search yet: the selected inboxes have no conversations.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
