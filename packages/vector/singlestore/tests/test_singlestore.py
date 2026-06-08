import os
import pathlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import cognee
from cognee.modules.search.types import SearchType
from cognee_community_vector_adapter_singlestore import register  # noqa: F401
from cognee_community_vector_adapter_singlestore.singlestore_adapter import (
    SingleStoreAdapter,
    SingleStoreDataPoint,
)
from cognee_community_vector_adapter_singlestore.SingleStoreDatasetDatabaseHandler import (
    SingleStoreDatasetDatabaseHandler,
)


class DeterministicEmbeddingEngine:
    def get_vector_size(self) -> int:
        return 3

    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        text = text.lower()
        if "hybrid" in text:
            return [0.8, 0.2, 0.0]
        if "quantum" in text:
            return [0.0, 1.0, 0.0]
        if "nlp" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0]


def singlestore_url() -> str:
    return os.getenv("SINGLESTORE_URL") or os.getenv("VECTOR_DB_URL") or "localhost:3306"


def singlestore_key() -> str:
    return (
        os.getenv("SINGLESTORE_KEY")
        or os.getenv("VECTOR_DB_KEY")
        or os.getenv("VECTOR_DB_PASSWORD")
        or ""
    )


async def test_adapter_lifecycle_and_filters():
    collection_name = f"adapter_collection_{uuid4().hex}"
    dataset_a = f"dataset_a_{uuid4().hex}"
    dataset_b = f"dataset_b_{uuid4().hex}"
    embedding_engine = DeterministicEmbeddingEngine()

    adapter_a = SingleStoreAdapter(
        url=singlestore_url(),
        api_key=singlestore_key(),
        database_name=dataset_a,
        embedding_engine=embedding_engine,
    )
    adapter_b = SingleStoreAdapter(
        url=singlestore_url(),
        api_key=singlestore_key(),
        database_name=dataset_b,
        embedding_engine=embedding_engine,
    )

    await adapter_a.prune()
    await adapter_b.prune()

    nlp_id = uuid4()
    hybrid_id = uuid4()
    quantum_id = uuid4()
    other_dataset_id = uuid4()

    await adapter_a.create_data_points(
        collection_name,
        [
            SingleStoreDataPoint(id=nlp_id, text="NLP document", belongs_to_set=["NLP"]),
            SingleStoreDataPoint(
                id=hybrid_id,
                text="Hybrid NLP quantum note",
                belongs_to_set=["NLP", "Quantum"],
            ),
            SingleStoreDataPoint(
                id=quantum_id,
                text="Quantum computers",
                belongs_to_set=["Quantum", "Computers"],
            ),
        ],
    )
    await adapter_b.create_data_points(
        collection_name,
        [
            SingleStoreDataPoint(
                id=other_dataset_id,
                text="NLP document from another dataset",
                belongs_to_set=["NLP"],
            )
        ],
    )

    assert await adapter_a.has_collection(collection_name)

    retrieved = await adapter_a.retrieve(collection_name, [str(nlp_id), str(hybrid_id)])
    assert {payload["id"] for payload in retrieved} == {nlp_id, hybrid_id}

    search_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=2,
    )
    assert search_results[0].payload["id"] == nlp_id

    or_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
        node_name=["NLP", "Quantum"],
        node_name_filter_operator="OR",
    )
    assert {result.payload["id"] for result in or_results} == {nlp_id, hybrid_id, quantum_id}

    and_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
        node_name=["NLP", "Quantum"],
        node_name_filter_operator="AND",
    )
    assert {result.payload["id"] for result in and_results} == {hybrid_id}

    batch_results = await adapter_a.batch_search(
        collection_name,
        query_texts=["NLP", "Quantum"],
        include_payload=True,
        limit=1,
    )
    assert len(batch_results) == 2
    assert all(len(result_group) == 1 for result_group in batch_results)

    tenant_b_results = await adapter_b.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
    )
    assert {result.payload["id"] for result in tenant_b_results} == {other_dataset_id}

    deleted = await adapter_a.delete_data_points(collection_name, [str(nlp_id)])
    assert deleted["deleted"] == 1
    assert await adapter_a.retrieve(collection_name, [str(nlp_id)]) == []

    await adapter_a.prune()
    assert await adapter_a.get_collection_names() == []
    assert len(await adapter_b.get_collection_names()) == 1

    await adapter_b.prune()
    assert await adapter_b.get_collection_names() == []


async def test_dataset_handler_metadata_and_delete():
    from cognee import config

    dataset_id = uuid4()
    config.set_vector_db_config(
        {
            "vector_db_provider": "singlestore",
            "vector_db_url": singlestore_url(),
            "vector_db_key": singlestore_key(),
        }
    )

    dataset_config = await SingleStoreDatasetDatabaseHandler.create_dataset(dataset_id, None)
    assert dataset_config["vector_database_provider"] == "singlestore"
    assert dataset_config["vector_database_name"] == str(dataset_id)
    assert dataset_config["vector_dataset_database_handler"] == "singlestore"

    adapter = SingleStoreAdapter(
        url=singlestore_url(),
        api_key=singlestore_key(),
        database_name=str(dataset_id),
        embedding_engine=DeterministicEmbeddingEngine(),
    )
    collection_name = f"dataset_delete_{uuid4().hex}"
    await adapter.create_data_points(
        collection_name,
        [SingleStoreDataPoint(id=uuid4(), text="NLP document", belongs_to_set=["NLP"])],
    )
    assert len(await adapter.get_collection_names()) == 1

    dataset_database = SimpleNamespace(
        vector_database_provider="singlestore",
        vector_database_url=singlestore_url(),
        vector_database_key=singlestore_key(),
        vector_database_name=str(dataset_id),
    )

    with patch(
        "cognee_community_vector_adapter_singlestore."
        "SingleStoreDatasetDatabaseHandler.create_vector_engine",
        return_value=adapter,
    ):
        await SingleStoreDatasetDatabaseHandler.delete_dataset(dataset_database)

    assert await adapter.get_collection_names() == []


async def test_cognee_recall_flow():
    os.environ.setdefault("VECTOR_DATASET_DATABASE_HANDLER", "singlestore")

    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "singlestore",
            "vector_db_url": singlestore_url(),
            "vector_db_key": singlestore_key(),
        }
    )
    cognee.config.set_graph_db_config({"graph_database_provider": "kuzu"})

    data_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".data_storage/test_singlestore")
        ).resolve()
    )
    cognee.config.data_root_directory(data_directory_path)
    cognee_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".cognee_system/test_singlestore")
        ).resolve()
    )
    cognee.config.system_root_directory(cognee_directory_path)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    explanation_file_path_nlp = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Natural_language_processing.txt",
    )
    explanation_file_path_quantum = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Quantum_computers.txt",
    )

    await cognee.add([explanation_file_path_nlp], "natural_language")
    await cognee.add([explanation_file_path_quantum], "quantum")
    await cognee.cognify(["quantum", "natural_language"])

    search_results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Tell me about Quantum computers",
        datasets=["quantum"],
    )
    assert len(search_results) != 0, "The search results list is empty."

    from cognee.infrastructure.databases.vector import get_vector_engine

    vector_engine = get_vector_engine()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    assert await vector_engine.get_collection_names() == []


async def main():
    await test_adapter_lifecycle_and_filters()
    await test_dataset_handler_metadata_and_delete()
    await test_cognee_recall_flow()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
