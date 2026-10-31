"""Crisp connector demo: "ask my chat inbox".

Syncs a website's conversations into cognee memory, then asks a question about them.
Run it again later: only conversations that changed are re-read, and removed
conversations are forgotten.

One-time setup (see the README)
-------------------------------
1. Set ``CRISP_WEBSITE_ID``, ``CRISP_IDENTIFIER`` and ``CRISP_KEY`` (your token), and
   ``CRISP_TIER=plugin`` if it is a Marketplace plugin token.
2. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_crisp import crisp_source

DATASET_NAME = "crisp_chat"

# write_disposition="merge" is REQUIRED: later runs only return the conversations that
#   changed, so the default "replace" would wipe the others on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
CRISP_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    website = os.environ.get("CRISP_WEBSITE_ID")
    identifier = os.environ.get("CRISP_IDENTIFIER")
    key = os.environ.get("CRISP_KEY")
    if not website or not identifier or not key:
        print("Set CRISP_WEBSITE_ID, CRISP_IDENTIFIER and CRISP_KEY first.")
        return

    # The connector takes everything as arguments; only this example reads the environment.
    source = crisp_source(
        website_id=website,
        identifier=identifier,
        key=key,
        tier=os.environ.get("CRISP_TIER", "website"),
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **CRISP_REMEMBER_KWARGS)
    print(result)
    # Counts only, never message text.
    print("sync stats:", source.cognee_sync_stats)

    try:
        answers = await cognee.recall(
            "What problems did visitors report, and how did we help them?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print("Nothing to search yet: the website has no conversations with messages.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
