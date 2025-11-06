"""Ladybug adapter usage example."""

import asyncio
import pathlib

import cognee
from cognee_community_graph_adapter_ladybug import register


async def main() -> None:
    register()

    base_path = pathlib.Path(__file__).parent
    db_file = base_path / ".examples_data" / "ladybug_graph.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)

    cognee.config.set_graph_database_provider("ladybug")
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": str(db_file),
        }
    )

    # Optional: ensure a clean workspace for the demo
    await cognee.prune.prune_data()
    await cognee.prune.prune_system()

    corpus = [
        "Ladybug is an embedded graph database optimized for analytics.",
        "It is a fork of the Kùzu project with additional performance improvements.",
    ]

    collection = "ladybug_demo"
    await cognee.add(corpus, dataset_name=collection)
    await cognee.cognify([collection])

    results = await cognee.search(
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        query_text="What is Ladybug?",
    )

    for idx, item in enumerate(results, start=1):
        print(f"{idx}. {item}")


if __name__ == "__main__":
    asyncio.run(main())
