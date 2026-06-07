"""Run the Alice in Wonderland pipeline against a graph backend.

Backend is chosen by GRAPH_DATABASE_PROVIDER:
  - "turbopuffer": uses this adapter (requires it to be IMPLEMENTED, plus
    TURBOPUFFER_API_KEY / TURBOPUFFER_REGION). Until the adapter is implemented
    this will raise NotImplementedError during cognify.
  - anything else / unset: uses cognee's default backend (Ladybug) so you can
    see the full flow + graph shape today.

Usage:
    # default backend (works now)
    python examples/example.py

    # turbopuffer (works once the adapter is implemented)
    GRAPH_DATABASE_PROVIDER=turbopuffer TURBOPUFFER_API_KEY=... \
    TURBOPUFFER_REGION=gcp-us-east4 python examples/example.py
"""

import asyncio
import os
import pathlib

import cognee
from cognee.modules.search.types import SearchType

ALICE_CANDIDATES = [
    pathlib.Path(__file__).parents[1] / "tests" / "data" / "alice_in_wonderland.txt",
    pathlib.Path(__file__).parents[6] / "notebooks" / "data" / "alice_in_wonderland.txt",
    pathlib.Path(__file__).parents[6]
    / "examples" / "demos" / "simple_document_qa" / "data" / "alice_in_wonderland.txt",
]


def resolve_alice() -> str:
    env = os.getenv("ALICE_DATA_PATH")
    if env and pathlib.Path(env).is_file():
        return env
    for c in ALICE_CANDIDATES:
        if c.is_file():
            return str(c.resolve())
    raise FileNotFoundError("Set ALICE_DATA_PATH to alice_in_wonderland.txt")


async def main():
    provider = os.getenv("GRAPH_DATABASE_PROVIDER", "").lower()
    if provider == "turbopuffer":
        from cognee_community_graph_adapter_turbopuffer import register

        register()
        cognee.config.set_graph_database_provider("turbopuffer")
        print(">> graph backend: turbopuffer")
    else:
        print(">> graph backend: default (ladybug)")

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    dataset = "wonderland"
    await cognee.add([resolve_alice()], dataset)
    await cognee.cognify([dataset])

    results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION, query_text="Who does Alice meet?"
    )
    print("\n=== GRAPH_COMPLETION ===")
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
