"""TickTick connector demo — turn your tasks into memory.

Pull TickTick projects and tasks into cognee, with forget-on-delete.
``ticktick_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Tasks and projects are ingested as normal documents
(so they go through the full cognify entity-extraction pipeline).

Each run is a full snapshot: unchanged items keep a stable content-hash id
and are not re-cognified, and items you delete in TickTick drop out of the
snapshot, so cognee's orphan cleanup forgets them on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your TickTick account. It is strictly opt-in —
nothing is fetched until you run this script. Scope what you ingest with
``selected_project_ids``, keep the token file private, and use a dedicated
dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package::

       cd packages/connector/ticktick && uv sync --all-extras

2. Register an OAuth app at https://developer.ticktick.com/manage with
   redirect URI ``http://localhost:8080/callback`` and scope ``tasks:read``.
3. Either:

   a. Export a token you already have::

          export TICKTICK_ACCESS_TOKEN="…"

   b. Or run the browser OAuth helper once (caches to ``.ticktick-token``)::

          export TICKTICK_CLIENT_ID="…"
          export TICKTICK_CLIENT_SECRET="…"

4. Export your LLM key and run::

       export LLM_API_KEY="sk-…"
       uv run python examples/example.py

Re-run after editing or deleting a task to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_ticktick import get_ticktick_token, ticktick_source

# Keep TickTick in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "ticktick"


async def main() -> None:
    access_token = os.environ.get("TICKTICK_ACCESS_TOKEN")
    if not access_token:
        if not (os.environ.get("TICKTICK_CLIENT_ID") and os.environ.get("TICKTICK_CLIENT_SECRET")):
            print(
                "Set TICKTICK_ACCESS_TOKEN, or TICKTICK_CLIENT_ID + "
                "TICKTICK_CLIENT_SECRET (and LLM_API_KEY) to run this example."
            )
            return
        print("No TICKTICK_ACCESS_TOKEN; starting browser OAuth flow…")
        access_token = get_ticktick_token()

    # Scope with selected_project_ids=["inbox", "…"]; omit to ingest everything
    # the token can see (all projects + Inbox).
    source = ticktick_source(access_token=access_token)

    print("Syncing TickTick into cognee …")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="replace",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What are my open tasks?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete a task in TickTick, then re-run: edits re-sync and "
        "deleted tasks are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
