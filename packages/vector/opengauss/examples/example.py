"""openGauss DataVec adapter example — ingest, cognify, and search."""

import asyncio
import os
import pathlib

from dotenv import load_dotenv

from cognee import SearchType, add, cognify, config, prune, search
from cognee_community_vector_adapter_opengauss import register  # noqa: F401 — registers adapter

load_dotenv()


async def main() -> None:
    # Step 1: Point cognee at a scratch directory for system data.
    root = pathlib.Path(__file__).parent
    config.system_root_directory(str(root / ".cognee_system"))
    config.data_root_directory(str(root / ".cognee_data"))

    # Step 2: Tell cognee to use openGauss as the vector store.
    opengauss_url = os.getenv(
        "OPENGAUSS_URL",
        "postgresql://gaussdb:OpenGauss%40123@localhost:5432/postgres",
    )
    config.set_vector_db_config({
        "vector_db_provider": "opengauss",
        "vector_db_url": opengauss_url,
    })

    host = opengauss_url.rsplit("@", 1)[-1].split("/", 1)[0]
    print(f"openGauss DataVec Demo — {host}")

    # Step 3: Clear leftover data from previous runs.
    try:
        await prune.prune_data()
        await prune.prune_system(metadata=True)
    except Exception:
        pass

    # Step 4: Add documents. Each call to `add` stores raw text.
    documents = [
        "Natural language processing (NLP) is an interdisciplinary subfield of computer science.",
        "Machine learning enables systems to learn from experience without explicit programming.",
        "Deep learning uses multiple layers to extract higher-level features from raw input.",
    ]
    for doc in documents:
        await add(doc)

    # Step 5: Run the cognify pipeline — chunk, embed, extract graph, index.
    print("Processing...")
    await cognify()

    # Step 6: Search across the knowledge graph and vector index.
    results = await search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Tell me about NLP",
    )

    if not results:
        print("No results found")
        return

    for i, result in enumerate(results, 1):
        print(f"\n--- Result #{i} ---")
        if isinstance(result, str):
            print(result[:500])
        elif isinstance(result, dict):
            print(result.get("search_result", str(result)))
        else:
            print(str(result))


if __name__ == "__main__":
    asyncio.run(main())
