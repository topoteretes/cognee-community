"""
Integration tests for Valkey adapter.
These tests require a running Valkey instance.
"""

import os
import uuid

import pytest
from cognee import config
from cognee.infrastructure.databases.vector import get_vector_engine
from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_valkey import register  # noqa: F401
from cognee_community_vector_adapter_valkey.exceptions import CollectionNotFoundError
from cognee_community_vector_adapter_valkey.valkey_adapter import MissingQueryParameterError
from dotenv import load_dotenv
from glide import ft
from glide_shared.constants import OK

load_dotenv()


class MyChunk(DataPoint):
    text: str
    metadata: dict = {
        "type": "DocumentChunk",
        "index_fields": ["text"],
    }


@pytest.fixture(scope="session", autouse=True)
async def valkey_client_and_engine_config(tmp_path_factory):
    # Create temporary directories for system and data roots
    base_tmp = tmp_path_factory.mktemp("cognee")
    system_path = base_tmp / ".cognee-system"
    data_path = base_tmp / ".cognee-data"
    system_path.mkdir()
    data_path.mkdir()

    # Configure Cognee
    config.system_root_directory(str(system_path))
    config.data_root_directory(str(data_path))
    config.set_vector_db_config(
        {
            "vector_db_provider": "valkey",
            "vector_db_url": os.getenv("VECTOR_DB_URL", "valkey://localhost:6379"),
        }
    )


@pytest.fixture()
async def valkey_client_and_engine():
    vector_engine = get_vector_engine()
    client = await vector_engine.get_connection()

    yield client, vector_engine

    # The adapter's glide client lives on its private I/O loop; raw glide
    # commands must be routed through _run_glide — awaiting them on the test's
    # loop never completes (cross-loop futures).
    # Drop all indexes before each test
    all_indexes = await vector_engine._run_glide(ft.list(client))
    for index in all_indexes:
        await vector_engine._run_glide(ft.dropindex(client, index))
    await vector_engine.close()


async def test_happy_path(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    # vector_engine = get_vector_engine()
    # client = await vector_engine.get_connection()
    collection = "my_collection-" + str(uuid.uuid4())

    # Create collection
    await vector_engine.create_collection(collection)

    # Verify collection created
    info = await vector_engine._run_glide(ft.info(client, vector_engine._index_name(collection)))
    assert info is not None

    # Insert a couple of points
    id_1 = uuid.uuid4()
    id_2 = uuid.uuid4()
    data_points = [
        MyChunk(id=id_1, text="Hello Valkey"),
        MyChunk(id=id_2, text="Ollama local embeddings are neat"),
    ]
    await vector_engine.create_data_points(collection, data_points)

    # Text search (the adapter should embed the query via the same engine)
    results = await vector_engine.search(collection_name=collection, query_text="Hello", limit=10)

    assert len(results) == 2
    assert [r.id for r in results] == [id_1, id_2]

    assert (
        await vector_engine._run_glide(ft.dropindex(client, vector_engine._index_name(collection)))
        == OK
    )


async def test_create_data_points_collection_not_found(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine

    with pytest.raises(CollectionNotFoundError):
        await vector_engine.create_data_points(
            collection_name="non_existing_collection",
            data_points=[MyChunk(id=str(uuid.uuid4()), text="Should fail")],
        )


async def test_create_data_points_empty_list(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"empty_insert_{uuid.uuid4()}"
    await vector_engine.create_collection(collection_name=collection)

    # Insert empty list
    await vector_engine.create_data_points(collection_name=collection, data_points=[])

    # Verify no data points exist
    results = await vector_engine.search(
        collection_name=collection, query_text="anything", limit=10
    )
    assert results == []


async def test_empty_collection_search_returns_no_results(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"empty_search_{uuid.uuid4()}"

    await vector_engine.create_collection(collection_name=collection)
    results = await vector_engine.search(
        collection_name=collection, query_text="Nonexistent", limit=10
    )

    assert results == []
    assert (
        await vector_engine._run_glide(ft.dropindex(client, vector_engine._index_name(collection)))
        == OK
    )


async def test_search_invalid_collection_returns_no_results(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine

    results = await vector_engine.search(
        collection_name="does_not_exist", query_text="Hello", limit=10
    )
    assert results == []


async def test_search_empty_query_text(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"empty_search_{uuid.uuid4()}"

    await vector_engine.create_collection(collection)

    with pytest.raises(MissingQueryParameterError):
        await vector_engine.search(collection_name=collection, query_text=None, limit=10)

    assert (
        await vector_engine._run_glide(ft.dropindex(client, vector_engine._index_name(collection)))
        == OK
    )


async def test_delete_data_points(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"delete_data_points_{uuid.uuid4()}"

    id_1 = uuid.uuid4()
    id_2 = uuid.uuid4()
    data_points = [
        MyChunk(id=id_1, text="Hello Valkey"),
        MyChunk(id=id_2, text="Ollama local embeddings are neat"),
    ]

    await vector_engine.create_collection(collection_name=collection)

    # Insert a couple of points
    await vector_engine.create_data_points(collection, data_points)

    # Text search (the adapter should embed the query via the same engine)
    results = await vector_engine.search(collection_name=collection, query_text="Hello", limit=10)
    assert len(results) == 2

    # Delete data points
    await vector_engine.delete_data_points(collection, [id_1, id_2])
    results = await vector_engine.search(collection_name=collection, query_text="Hello", limit=10)
    assert len(results) == 0

    # Insert data points again
    await vector_engine.create_data_points(collection, data_points)

    # Delete data points
    await vector_engine.delete_data_points(collection, [id_1])
    results = await vector_engine.search(collection_name=collection, query_text="Hello", limit=10)
    assert len(results) == 1
    assert [r.id for r in results] == [id_2]

    assert (
        await vector_engine._run_glide(ft.dropindex(client, vector_engine._index_name(collection)))
        == OK
    )


async def test_delete_non_existing_ids(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"delete_non_existing_{uuid.uuid4()}"
    await vector_engine.create_collection(collection_name=collection)

    # Attempt to delete IDs that don't exist
    result = await vector_engine.delete_data_points(
        collection_name=collection, data_point_ids=["fake-id-1", "fake-id-2"]
    )
    assert "deleted" in result
    assert result["deleted"] == 0


async def test_retrieve_data_points(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"delete_data_points_{uuid.uuid4()}"

    id_1 = uuid.uuid4()
    id_2 = uuid.uuid4()
    data_points = [
        MyChunk(id=id_1, text="Hello Valkey"),
        MyChunk(id=id_2, text="Ollama local embeddings are neat"),
    ]

    await vector_engine.create_collection(collection)

    # Insert a couple of points
    await vector_engine.create_data_points(collection, data_points)

    # Retrieve data points
    results = await vector_engine.retrieve(collection, [id_1])
    print(f"TestLog: retrieve: {results}")
    assert len(results) == 1
    assert [r["id"] for r in results] == [str(id_1)]

    # Retrieve data points again
    results = await vector_engine.retrieve(collection, [id_1, id_2])
    assert len(results) == 2
    assert [r["id"] for r in results] == [str(id_1), str(id_2)]


async def test_prune_removes_all_collections(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine

    # Create two collections
    col1 = f"test_prune_{uuid.uuid4()}"
    col2 = f"test_prune_{uuid.uuid4()}"
    await vector_engine.create_collection(collection_name=col1)
    await vector_engine.create_collection(collection_name=col2)

    # Verify collections exist
    indexes_before = [
        idx.decode("utf-8") for idx in await vector_engine._run_glide(ft.list(client))
    ]
    assert vector_engine._index_name(col1) in indexes_before
    assert vector_engine._index_name(col2) in indexes_before

    # Call prune
    await vector_engine.prune()

    # Verify all collections are removed
    indexes_after = [idx.decode("utf-8") for idx in await vector_engine._run_glide(ft.list(client))]
    assert vector_engine._index_name(col1) not in indexes_after
    assert vector_engine._index_name(col2) not in indexes_after
    assert len(indexes_after) == 0 or all(idx.startswith("system") for idx in indexes_after)


async def test_prune_raises_on_error(valkey_client_and_engine, monkeypatch):
    client, vector_engine = valkey_client_and_engine

    async def mock_list_fail(*args, **kwargs):
        raise Exception("Simulated failure")

    monkeypatch.setattr("glide.ft.list", mock_list_fail)

    with pytest.raises(Exception, match="Simulated failure"):
        await vector_engine.prune()


async def test_batch_search_returns_results(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"batch_search_{uuid.uuid4()}"
    await vector_engine.create_collection(collection_name=collection)

    # Insert data points
    id_1 = uuid.uuid4()
    id_2 = uuid.uuid4()
    data_points = [
        MyChunk(id=str(id_1), text="Hello Valkey"),
        MyChunk(id=str(id_2), text="Ollama embeddings are neat"),
    ]
    await vector_engine.create_data_points(collection_name=collection, data_points=data_points)

    # Perform batch search. No score_threshold: absolute distances depend on
    # the embedding model in use (e.g. OpenAI cosine distances for these
    # sentences exceed 0.5), so the assertions below rely on membership and
    # ranking rather than a model-specific cutoff.
    queries = ["Hello", "embeddings"]
    results = await vector_engine.batch_search(
        collection_name=collection, query_texts=queries, limit=10
    )

    # Validate structure
    assert isinstance(results, list)
    assert len(results) == len(queries)
    assert all(isinstance(group, list) for group in results)

    # Validate at least one result per query
    assert any(r.id == id_1 for r in results[0])
    assert any(r.id == id_2 for r in results[1])


async def test_batch_search_empty_queries(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    collection = f"batch_search_empty_{uuid.uuid4()}"
    await vector_engine.create_collection(collection_name=collection)

    results = await vector_engine.batch_search(collection_name=collection, query_texts=[], limit=10)
    assert results == []


async def test_batch_search_non_existing_collection(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine
    results = await vector_engine.batch_search(
        collection_name="does_not_exist", query_texts=["Hello"], limit=10
    )

    assert results == []


async def test_valkey_connection(valkey_client_and_engine):
    client, vector_engine = valkey_client_and_engine

    assert client is not None
    assert (await vector_engine._run_glide(client.ping())) in (b"PONG", "PONG")
