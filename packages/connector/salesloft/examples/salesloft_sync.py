"""Salesloft connector demo — turn your sales engagement data into memory.

Pull Salesloft notes, calls, and cadences into cognee, with incremental sync
and forget-on-delete. ``salesloft_source`` returns a ``dlt`` source you hand
straight to ``cognee.remember``.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Salesloft notes and call recordings. It is
strictly opt-in — nothing is fetched until you run this script. Scope what
you ingest with ``resources`` and ``cadence_ids``, and use a dedicated dataset
so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create a Salesloft OAuth application at
   https://accounts.salesloft.com/oauth/applications

2. Run the OAuth flow to get tokens (see README.md for details).

3. Export your tokens and LLM key:

       export SALESLOFT_ACCESS_TOKEN="..."
       export LLM_API_KEY="sk-..."
       python examples/salesloft_sync.py

Re-run after changes in Salesloft to see incremental sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_salesloft import salesloft_source


DATASET_NAME = "salesloft"


async def main() -> None:
    if not os.environ.get("SALESLOFT_ACCESS_TOKEN"):
        print(
            "Set SALESLOFT_ACCESS_TOKEN (or configure OAuth credentials) to run this example.\n"
            "See README.md for setup instructions."
        )
        return

    # Scope with resources= and cadence_ids=; defaults to all resources.
    source = salesloft_source(
        resources=["notes", "calls", "cadences"],
        # cadence_ids=[123],  # Uncomment to scope to specific cadences.
    )

    print("Syncing Salesloft records into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What objections did prospects raise?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete records in Salesloft, then re-run: edits re-sync "
        "and deleted records are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())