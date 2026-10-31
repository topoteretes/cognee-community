"""Goodreads connector demo: "ask my library".

Syncs your Goodreads library export into cognee memory, then asks a question
about it. Run it again with a newer export: changed books are upserted,
unchanged books keep their content hash (no re-cognify), and books you removed
since the previous export are forgotten.

One-time setup
--------------
1. On goodreads.com: Settings -> Import/Export -> "Export Library" and wait
   for the CSV (usually named ``goodreads_library_export.csv``) to download.
2. Set ``GOODREADS_EXPORT`` to the path of that CSV file.
3. Set your ``LLM_API_KEY`` (as for any cognee run).

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee
from cognee.modules.retrieval.exceptions.exceptions import NoDataError

from cognee_community_connector_goodreads import goodreads_source

DATASET_NAME = "goodreads"

# write_disposition="merge" is REQUIRED: every run re-reads the whole snapshot
#   and the pipeline default ("replace") would wipe earlier books.
# max_rows_per_table=0 makes forget-on-delete compare against every stored row.
GOODREADS_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
    "self_improvement": False,
}


async def main():
    export = os.environ.get("GOODREADS_EXPORT")
    if not export:
        print("Set GOODREADS_EXPORT first (see the setup steps at the top of this file).")
        return
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY first (as for any cognee run).")

    print(f"Syncing {export} ...")
    result = await cognee.remember(
        goodreads_source(export=export),
        dataset_name=DATASET_NAME,
        **GOODREADS_REMEMBER_KWARGS,
    )
    print(result)

    try:
        answers = await cognee.recall(
            "Which books did I read about systems design, and what did I think of them?",
            datasets=[DATASET_NAME],
        )
    except NoDataError:  # the dataset is still empty
        print(
            "Nothing to search yet: the export has no books. Check that "
            "GOODREADS_EXPORT points at a real Goodreads library export, then run again."
        )
        return
    for answer in answers:
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
