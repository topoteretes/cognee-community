"""HubSpot connector demo: "ask my CRM".

Syncs contacts, companies, deals, tickets and notes, with the relationships between
them, into cognee memory, then asks a question about them. Run it again later: only
records that changed are re-read, documents that name a changed record are refreshed,
and deleted or merged records are forgotten.

One-time setup
--------------
1. Credentials, either:
   - ``HUBSPOT_ACCESS_TOKEN``: a Service Key or private app token, or
   - ``HUBSPOT_CLIENT_ID``, ``HUBSPOT_CLIENT_SECRET`` and ``HUBSPOT_REFRESH_TOKEN`` for an
     OAuth app (a fresh access token is minted on each run). Get the refresh token once
     with ``uv run python examples/get_refresh_token.py``.
   Grant the read scopes listed in the README for the objects you sync.
2. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it, optionally with your own question:

    uv run python examples/example.py ["Which deals is Jane Doe on?"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_hubspot import hubspot_source

DATASET_NAME = "hubspot_crm"

# write_disposition="merge" is REQUIRED: the cursor only returns changed records,
#   so the default "replace" would wipe earlier records on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
HUBSPOT_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    if not (os.environ.get("HUBSPOT_ACCESS_TOKEN") or os.environ.get("HUBSPOT_REFRESH_TOKEN")):
        print("Set HubSpot credentials first (see the top of this file).")
        return

    result = await cognee.remember(
        hubspot_source(objects=["contacts", "companies", "deals", "tickets", "notes"]),
        dataset_name=DATASET_NAME,
        **HUBSPOT_REMEMBER_KWARGS,
    )
    print(result)

    question = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Which companies and deals is each contact connected to, and what do the notes say?"
    )
    print("Question:", question)
    try:
        answers = await cognee.recall(question, datasets=[DATASET_NAME])
    except Exception as error:  # cognee raises NoDataError while the dataset is empty
        if type(error).__name__ != "NoDataError":
            raise
        print("Nothing to search yet: no records were found for the selected objects.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
