"""End-to-end pipeline test for the TurboPuffer graph adapter on Alice in Wonderland.

Faithfully mirrors cognee/tests/e2e/postgres/test_graphdb_shared.py
(``run_graph_db_test``) but:
  - uses the TurboPuffer graph provider,
  - loads ``alice_in_wonderland.txt`` as the source document,
  - keeps the default vector backend (LanceDB) so this isolates the graph adapter.

It walks the entire flow that production uses:
  prune -> add(alice) -> assert graph empty before cognify -> cognify
  -> assert graph populated -> vector lookup -> GRAPH_COMPLETION / CHUNKS /
  SUMMARIES searches -> search-history accounting -> node_set filtering via
  GraphCompletionRetriever -> prune -> assert empty.

Requires TURBOPUFFER_API_KEY, LLM_API_KEY and COGNEE_TURBOPUFFER_GRAPH_E2E=1.
"""

import asyncio
import os
import pathlib
import shutil

import pytest
from conftest import requires_e2e

# Candidate locations for the Alice corpus (package-local first, then repo copies).
_ALICE_CANDIDATES = [
    pathlib.Path(__file__).parent / "data" / "alice_in_wonderland.txt",
    pathlib.Path(__file__).parents[5] / "notebooks" / "data" / "alice_in_wonderland.txt",
    pathlib.Path(__file__).parents[5]
    / "examples"
    / "demos"
    / "simple_document_qa"
    / "data"
    / "alice_in_wonderland.txt",
]


def _resolve_alice_path() -> str:
    env = os.getenv("ALICE_DATA_PATH")
    if env and pathlib.Path(env).is_file():
        return env
    for c in _ALICE_CANDIDATES:
        if c.is_file():
            return str(c.resolve())
    raise FileNotFoundError(
        "alice_in_wonderland.txt not found. Set ALICE_DATA_PATH to point at it."
    )


async def run_alice_pipeline(provider: str = "turbopuffer"):
    """Full add -> cognify -> search pipeline against the given graph provider."""
    import cognee
    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee.infrastructure.files.storage import get_storage_config
    from cognee.modules.engine.models import NodeSet
    from cognee.modules.retrieval.graph_completion_retriever import GraphCompletionRetriever
    from cognee.modules.search.operations import get_history
    from cognee.modules.search.types import SearchType
    from cognee.modules.users.methods import get_default_user
    from cognee_community_graph_adapter_turbopuffer import register

    register()  # make "turbopuffer" graph provider resolvable

    base = pathlib.Path(__file__).parent
    data_dir = str((base / f".data_storage/test_{provider}").resolve())
    system_dir = str((base / f".cognee_system/test_{provider}").resolve())

    graph_config = get_graph_config()
    base_config = get_base_config()
    prev_provider = graph_config.graph_database_provider
    prev_data_root = base_config.data_root_directory
    prev_system_root = base_config.system_root_directory

    try:
        cognee.config.set_graph_database_provider(provider)
        cognee.config.data_root_directory(data_dir)
        cognee.config.system_root_directory(system_dir)

        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)

        dataset_name = "wonderland"
        alice_path = _resolve_alice_path()

        from cognee.infrastructure.databases.graph import get_graph_engine

        graph_engine = await get_graph_engine()

        await cognee.add([alice_path], dataset_name)

        # Graph empty before cognify.
        assert await graph_engine.is_empty(), f"{provider}: graph should be empty before cognify"

        # Full knowledge-graph construction (LLM extraction -> graph + vector).
        await cognee.cognify([dataset_name])

        assert not await graph_engine.is_empty(), (
            f"{provider}: graph should not be empty after cognify"
        )

        # Pick a real node name out of the vector index to query the graph with.
        from cognee.infrastructure.databases.vector import get_vector_engine

        vector_engine = get_vector_engine()
        random_node = (await vector_engine.search("Entity_name", "Alice", include_payload=True))[0]
        random_node_name = random_node.payload["text"]

        # GRAPH_COMPLETION exercises the graph adapter's traversal/connection path.
        results = await cognee.search(
            query_type=SearchType.GRAPH_COMPLETION, query_text=random_node_name
        )
        assert len(results) != 0, f"{provider}: GRAPH_COMPLETION returned no results"

        # CHUNKS + SUMMARIES confirm overall pipeline integrity (vector path).
        results = await cognee.search(query_type=SearchType.CHUNKS, query_text=random_node_name)
        assert len(results) != 0, f"{provider}: CHUNKS returned no results"

        results = await cognee.search(query_type=SearchType.SUMMARIES, query_text=random_node_name)
        assert len(results) != 0, f"{provider}: SUMMARIES returned no results"

        # 3 searches x (query + result) = 6 history rows.
        user = await get_default_user()
        history = await get_history(user.id)
        assert len(history) == 6, f"{provider}: expected 6 history entries, got {len(history)}"

        # node_set filtering through the retriever (exercises get_nodeset_subgraph).
        await cognee.add(
            ["The Cheshire Cat grinned at Alice from the tree."],
            dataset_name,
            node_set=["first"],
        )
        await cognee.cognify([dataset_name])

        retriever = GraphCompletionRetriever(node_type=NodeSet, node_name=["first"])
        objects = await retriever.get_retrieved_objects("What is in the context?")
        context_nonempty = await retriever.get_context_from_objects(
            query="What is in the context?", retrieved_objects=objects
        )

        retriever = GraphCompletionRetriever(node_type=NodeSet, node_name=["nonexistent"])
        objects = await retriever.get_retrieved_objects("What is in the context?")
        context_empty = await retriever.get_context_from_objects(
            query="What is in the context?", retrieved_objects=objects
        )

        assert isinstance(context_nonempty, str) and context_nonempty != "", (
            f"{provider}: expected non-empty context for existing node_set"
        )
        assert context_empty == "", (
            f"{provider}: expected empty context for nonexistent node_set, got {context_empty!r}"
        )

        # Prune removes local data + empties the graph.
        await cognee.prune.prune_data()
        data_root = get_storage_config()["data_root_directory"]
        assert not os.path.isdir(data_root), f"{provider}: local data files not deleted"

        await cognee.prune.prune_system(metadata=True)
        assert await graph_engine.is_empty(), f"{provider}: graph should be empty after prune"

    finally:
        cognee.config.set_graph_database_provider(prev_provider)
        cognee.config.data_root_directory(prev_data_root)
        cognee.config.system_root_directory(prev_system_root)
        for path in (data_dir, system_dir):
            if os.path.exists(path):
                shutil.rmtree(path)


@requires_e2e
@pytest.mark.asyncio
async def test_alice_full_pipeline():
    await run_alice_pipeline("turbopuffer")


if __name__ == "__main__":
    # Allow running as a standalone script, like cognee's e2e entry points.
    asyncio.run(run_alice_pipeline("turbopuffer"))
