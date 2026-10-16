"""xkcd connector demo — turn the xkcd archive into memory ("ask xkcd").

Pull xkcd comics into cognee and query them. ``xkcd_source`` returns a ``dlt``
resource you hand straight to ``cognee.remember``. Each comic is ingested as a
normal document (title, publication date, image link, alt text, and
transcript), so it goes through the full cognify entity-extraction pipeline.

No credentials are needed — the xkcd JSON API is public. The first run backfills
the archive (paced, one request every 0.5 s); later runs fetch only comics
published after the stored watermark. Pass ``write_disposition="merge"`` so a
re-sync upserts by comic id instead of rewriting staging with only the delta.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:

       pip install cognee-community-connector-xkcd
       # or, from this monorepo: uv sync

2. Export your LLM key (cognify/search need one), then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run to pull only new comics; the dataset stays a merge of the whole archive.
"""

import asyncio
import os

import cognee

from cognee_community_connector_xkcd import xkcd_source

# Keep xkcd in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "xkcd"


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to cognify and search the comics.")
        return

    # Bound the first backfill to the last couple dozen comics for a quick
    # demo; drop since_num to ingest the whole archive (~3300 comics).
    source = xkcd_source(since_num=3280)

    print("Syncing xkcd comics into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        write_disposition="merge",  # incremental upsert by comic id
        max_rows_per_table=0,  # reconcile the whole corpus (no read-back cap)
    )

    answer = await cognee.search(
        query_text="Which comics mention woodpeckers, and what is the joke?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run later: only comics newer than the stored watermark are fetched, "
        "and rows are merged by id so the corpus stays intact."
    )


if __name__ == "__main__":
    asyncio.run(main())
