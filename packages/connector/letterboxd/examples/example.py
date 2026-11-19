"""Letterboxd connector demo: "ask my film history".

Syncs your Letterboxd export into cognee memory, then asks a question about
it. Run it again with a newer export: changed films are upserted, unchanged
films keep their content hash (no re-cognify), and films or lists you removed
since the previous export are forgotten.

One-time setup
--------------
1. On letterboxd.com: Settings -> Import & Export -> Request Export. The ZIP
   arrives by email (it can take a few minutes to a day).
2. Set ``LETTERBOXD_EXPORT`` to the path of the downloaded ``.zip`` (or the
   folder you extracted it into).
3. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_letterboxd import letterboxd_source

DATASET_NAME = "letterboxd"

# write_disposition="merge" is REQUIRED: every run re-reads the whole snapshot
#   and the pipeline default ("replace") would wipe earlier films.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
LETTERBOXD_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    export = os.environ.get("LETTERBOXD_EXPORT")
    if not export:
        print("Set LETTERBOXD_EXPORT first (see the setup steps at the top of this file).")
        return

    print(f"Syncing {export} ...")
    result = await cognee.remember(
        letterboxd_source(export=export),
        dataset_name=DATASET_NAME,
        **LETTERBOXD_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "Which films did I rate highest, and what did I think of them?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print(
            "Nothing to search yet: the export has no films. Check that "
            "LETTERBOXD_EXPORT points at a real export ZIP, then run this again."
        )
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
