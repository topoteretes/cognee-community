"""Zoho Desk connector demo: "ask my help desk".

Syncs tickets and published knowledge-base articles into cognee memory, then asks a
question about them. Run it again later: only what changed is re-read, and deleted
tickets and articles are forgotten.

One-time setup (see the README for the details)
-----------------------------------------------
1. Create a Self Client in the Zoho API console and exchange a code for a refresh token.
2. Set ``ZOHO_DESK_DATA_CENTER`` (``com``, ``eu``, ``in``, ...), ``ZOHO_DESK_CLIENT_ID``,
   ``ZOHO_DESK_CLIENT_SECRET`` and ``ZOHO_DESK_REFRESH_TOKEN``. Optionally set
   ``ZOHO_DESK_DEPARTMENTS`` to a comma-separated list of department ids.
3. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_zoho_desk import zoho_desk_source

DATASET_NAME = "zoho_desk"

# write_disposition="merge" is REQUIRED: later runs only return what changed, so the
#   default "replace" would wipe the rest on the second sync.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
ZOHO_DESK_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    env = {
        k: os.environ.get(f"ZOHO_DESK_{k}") for k in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
    }
    if not all(env.values()):
        print("Set ZOHO_DESK_CLIENT_ID, ZOHO_DESK_CLIENT_SECRET and ZOHO_DESK_REFRESH_TOKEN first.")
        return
    departments = [
        d.strip() for d in os.environ.get("ZOHO_DESK_DEPARTMENTS", "").split(",") if d.strip()
    ]

    # The connector takes everything as arguments; only this example reads the environment.
    source = zoho_desk_source(
        data_center=os.environ.get("ZOHO_DESK_DATA_CENTER", "com"),
        client_id=env["CLIENT_ID"],
        client_secret=env["CLIENT_SECRET"],
        refresh_token=env["REFRESH_TOKEN"],
        departments=departments or None,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **ZOHO_DESK_REMEMBER_KWARGS)
    print(result)
    # Counts only, never ticket text.
    print("sync stats:", source.cognee_sync_stats)

    try:
        answers = await cognee.recall(
            "What problems did customers report, and how did we fix them?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print("Nothing to search yet: no tickets or published articles were found.")
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
