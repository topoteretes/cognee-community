"""Evernote connector demo — turn your Evernote account into memory.

Pull your Evernote notes into cognee, incrementally, with forget-on-delete.
``evernote_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``
— no routing kwargs needed beyond ``write_disposition``.

Run it three times to see the whole lifecycle:
  1. first run   — every note is ingested and searchable;
  2. edit a note — the re-sync ingests only that note;
  3. delete a note — the next sync removes it from memory entirely.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Evernote notes. It is strictly opt-in — nothing is
fetched until you run this script. Scope what you ingest with ``notebook_guids`` /
``tag_names``, and use a dedicated dataset so you can wipe it with a single
``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Request an Evernote API key at https://dev.evernote.com/portal/manage and export
   it, then authorize:

       export EVERNOTE_CONSUMER_KEY="..."
       uv run python examples/authorize.py

   Already have a developer token for your own account? Skip the above and just:

       export EVERNOTE_AUTH_TOKEN="S=s1:..."

2. Set your LLM key like any other cognee run:

       export LLM_API_KEY="sk-..."

3. Run:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_evernote import evernote_source

# Keep Evernote in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "evernote"

# Sync only what matters to you. Leave both empty to ingest every notebook.
NOTEBOOK_GUIDS: list[str] = []
TAG_NAMES: list[str] = []


def _have_credentials() -> bool:
    if os.environ.get("EVERNOTE_AUTH_TOKEN"):
        return True
    from cognee_community_connector_evernote import load_cached_token

    return bool(load_cached_token())


async def main() -> None:
    if not _have_credentials():
        print("No Evernote credentials found.")
        print("Run `uv run python examples/authorize.py`, or set EVERNOTE_AUTH_TOKEN.")
        return

    source = evernote_source(
        notebook_guids=NOTEBOOK_GUIDS or None,
        tag_names=TAG_NAMES or None,
    )

    print("Syncing Evernote notes into cognee ...")
    # write_disposition="merge" is REQUIRED: the add pipeline defaults to
    # "replace", which would wipe everything ingested so far on the second run.
    # max_rows_per_table=0 disables the default 50-row read cap so orphan cleanup
    # compares against the whole corpus.
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Summarize what these Evernote notes are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nNext: edit a note in Evernote and re-run this script — only that note is\n"
        "re-ingested. Then delete a note and re-run again — it is forgotten from\n"
        "memory (graph and vector stores) on that same run."
    )


if __name__ == "__main__":
    asyncio.run(main())
