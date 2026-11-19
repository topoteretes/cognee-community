"""Fathom connector demo: ask your meetings what was decided.

Pull your Fathom meetings (summary, action items, and optionally the transcript) into
cognee memory, incrementally, with forget-on-delete. ``fathom_source`` returns a ``dlt``
resource that you hand straight to ``cognee.remember``. The first run backfills every
meeting the key can see; re-running re-fetches only what is new, recent, or whose action
items changed, and a meeting deleted in Fathom is forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your meetings. It is strictly opt-in: nothing is fetched until
you run this script. Transcripts are off by default. E-mail addresses are never stored.
Use a dedicated dataset, so it is easy to share deliberately and to wipe with one
``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-fathom

2. Create an API key in Fathom under User Settings → API Access (every plan has it).

3. Export it, and your LLM key (``LLM_API_KEY``) like any other cognee example:

       export FATHOM_API_KEY="…"

Run it:

    python examples/example.py

Run it again after completing an action item or deleting a call in Fathom, to see the
re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_fathom import fathom_source

# keep meetings in their own dataset so it is easy to inspect, share and forget
DATASET_NAME = "fathom_meetings"
# one fixed name per Fathom API key: it keys the sync state and the forget-on-delete scope
RESOURCE_NAME = "fathom_meetings_main"

# merge is required: the add pipeline defaults to "replace", which would forget every
# meeting that did not change (the source refuses to run that way)
FATHOM_REMEMBER_KWARGS = {"primary_key": "id", "write_disposition": "merge"}

QUESTION = "What did we agree with Acme on pricing, and who owns the follow-up?"


async def sync(api_key: str) -> None:
    source = fathom_source(
        api_key=api_key,
        resource_name=RESOURCE_NAME,
        include_summary=True,
        include_action_items=True,
        include_transcript=False,  # opt in: transcripts make a meeting many times larger
        recheck_days=14,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **FATHOM_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main() -> None:
    api_key = os.environ.get("FATHOM_API_KEY")
    if not api_key:
        print("Set FATHOM_API_KEY (Fathom → User Settings → API Access) to run this example.")
        return

    print("Syncing Fathom meetings into cognee ...")
    await sync(api_key)

    answer = await cognee.search(
        query_text=QUESTION,
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(f"\nQ: {QUESTION}\nA: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
