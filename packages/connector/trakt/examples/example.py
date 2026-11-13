"""Trakt connector demo: "where did I leave off?".

Syncs your Trakt watched history, ratings, watchlist and personal lists into
cognee memory, then asks a question about them. Run it again later: only the
sections whose activity timestamps moved are re-pulled, rewatches upsert the
same rows, and a movie removed from your history is forgotten.

One-time setup
--------------
1. Create a Trakt API app at https://app.trakt.tv/settings/apps/api/new
   (redirect URI ``urn:ietf:wg:oauth:2.0:oob``) and copy the client id and
   client secret.
2. Get a user access token with the built-in PIN flow::

       uv run python -c "from cognee_community_connector_trakt import trakt_pin_flow; \
           trakt_pin_flow('YOUR_CLIENT_ID', 'YOUR_CLIENT_SECRET')"

   It prints a URL (open it, approve, copy the PIN), then the token JSON.
3. Set the environment variables below (plus ``LLM_API_KEY`` as for any
   cognee run). Pass the refresh material too and the connector refreshes
   the token itself whenever it expires.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_trakt import trakt_source

DATASET_NAME = "trakt_history"

# write_disposition="merge" is REQUIRED: incremental runs emit only changed
#   sections and deletions, so the default "replace" would wipe earlier syncs.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
TRAKT_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


def _int_env(name):
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


async def main():
    client_id = os.environ.get("TRAKT_CLIENT_ID", "").strip()
    access_token = os.environ.get("TRAKT_ACCESS_TOKEN", "").strip()
    if not client_id or not access_token:
        print(
            "Set TRAKT_CLIENT_ID and TRAKT_ACCESS_TOKEN first (see the setup steps at the "
            "top of this file — trakt_pin_flow prints the token you need)."
        )
        return

    print("Syncing your Trakt history ...")
    result = await cognee.remember(
        trakt_source(
            client_id,
            access_token,
            refresh_token=os.environ.get("TRAKT_REFRESH_TOKEN") or None,
            client_secret=os.environ.get("TRAKT_CLIENT_SECRET") or None,
            token_created_at=_int_env("TRAKT_TOKEN_CREATED_AT"),
            token_expires_in=_int_env("TRAKT_TOKEN_EXPIRES_IN"),
            username=os.environ.get("TRAKT_USERNAME") or None,
        ),
        dataset_name=DATASET_NAME,
        **TRAKT_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "What shows was I watching recently, and where did I leave off?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print(
            "Nothing to search yet: your Trakt history is empty. Watch (or rate) something, "
            "then run this again."
        )
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
