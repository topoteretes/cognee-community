"""Lattice connector demo: turn your Lattice goals into memory you can ask questions of.

Syncs the goals and review cycles of a Lattice workspace into cognee, then asks
a question. Re-run it after editing, archiving or deleting a goal in Lattice:
changed goals are re-read, and deleted or archived ones are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This sends the text of what you select to your configured LLM and embedding
providers during cognify. Only goals and review-cycle metadata are read by
default. Performance feedback and review responses are the most sensitive data
in Lattice, so each needs an explicit opt-in below. Use a dedicated dataset so
you can forget it all in one call.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install:  cd packages/connector/lattice && uv sync
2. Create an API key in the Lattice admin dashboard. It carries the privileges
   of the user who creates it, so make it from an account that can see only
   what you want in memory.
3. Export the key and your LLM key:

       export LATTICE_API_KEY="..."
       export LLM_API_KEY="sk-..."
       export LATTICE_REGION=emea                  # optional: "us" (default) or "emea"
       export LATTICE_INCLUDE_FEEDBACK=1           # optional, sensitive: company-shared feedback
       export LATTICE_REVIEW_CYCLE_IDS="id1,id2"   # optional, sensitive: review responses

4. Run:  uv run python examples/example.py
   The first run prints your review cycles with their ids, so you can pick
   which ones to put in LATTICE_REVIEW_CYCLE_IDS.
"""

import asyncio
import os

import cognee

from cognee_community_connector_lattice import LatticeClient, lattice_source

# Keep Lattice in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "lattice"


async def main() -> None:
    if not os.getenv("LATTICE_API_KEY"):
        print("Set LATTICE_API_KEY to run this example (see its docstring).")
        return

    cycle_ids = [
        c.strip() for c in os.getenv("LATTICE_REVIEW_CYCLE_IDS", "").split(",") if c.strip()
    ]
    if not cycle_ids:
        client = LatticeClient()
        print("Review cycles (put an id in LATTICE_REVIEW_CYCLE_IDS to sync its responses):")
        for cycle in client.list_all("/v1/reviewCycles"):
            print(f"  {cycle['id']}  {cycle.get('stage')}  {cycle.get('name')}")
        client.close()

    source = lattice_source(
        include_feedback=bool(os.getenv("LATTICE_INCLUDE_FEEDBACK")),  # off unless asked for
        review_cycle_ids=cycle_ids or None,  # None = no review responses
    )

    print("Syncing Lattice into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Which goals are off track, and who owns them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit, archive or delete a goal in Lattice, then re-run: changes re-sync and "
        "removed goals are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
