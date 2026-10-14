"""Zendesk connector demo: "ask my support desk".

Syncs tickets (with their comment threads) and Help Center articles into cognee
memory, then asks a question about them. Run it again later: only tickets that
changed are re-read, edited articles are replaced, and deleted tickets or
archived articles are forgotten.

One-time setup
--------------
1. Set ``ZENDESK_SUBDOMAIN`` (the ``acme`` in ``acme.zendesk.com``).
2. Set credentials, either:
   - ``ZENDESK_OAUTH_TOKEN``: an OAuth access token with the ``read`` scope
     (required for accounts that can no longer create API tokens), or
   - ``ZENDESK_EMAIL`` + ``ZENDESK_API_TOKEN`` for an existing API token.
3. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zendesk import zendesk_source

DATASET_NAME = "zendesk_support"

# write_disposition="merge" is REQUIRED: the export only returns changed tickets,
#   so the default "replace" would wipe earlier tickets on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
ZENDESK_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    if not os.environ.get("ZENDESK_SUBDOMAIN"):
        print("Set ZENDESK_SUBDOMAIN and credentials first (see the top of this file).")
        return

    result = await cognee.remember(
        zendesk_source(),
        dataset_name=DATASET_NAME,
        **ZENDESK_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "What problems did customers report, and how were they resolved?",
            datasets=[DATASET_NAME],
        )
    except Exception as error:  # cognee raises NoDataError while the dataset is empty
        if type(error).__name__ != "NoDataError":
            raise
        print("Nothing to search yet: no tickets or published articles were found.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
