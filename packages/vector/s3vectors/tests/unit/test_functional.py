"""Mocked functional tests for S3VectorsAdapter. No AWS account, no secrets."""

import json
import uuid
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_s3vectors.s3vectors_adapter import (
    S3VectorsAdapter,
)


class FakeEmbeddingEngine:
    def __init__(self, dims=4):
        self.dims = dims

    async def embed_text(self, texts):
        return [[0.1] * self.dims for _ in texts]

    def get_vector_size(self):
        return self.dims

    def get_batch_size(self):
        return 100


class FakeDataPoint(DataPoint):
    text: str


def make_data_point(id, text, belongs_to_set=None):
    point = FakeDataPoint(text=text)
    point.id = uuid.UUID(id)
    point.belongs_to_set = belongs_to_set or []
    point.metadata["index_fields"] = ["text"]
    return point


@pytest.fixture
def mock_boto_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)
    return client


@pytest.fixture
def adapter(mock_boto_client):
    return S3VectorsAdapter(
        database_name="test-bucket",
        embedding_engine=FakeEmbeddingEngine(),
        region_name="us-east-1",
    )


def _not_found_error(op_name):
    return ClientError({"Error": {"Code": "NotFoundException", "Message": "not found"}}, op_name)


@pytest.mark.asyncio
async def test_has_collection_true(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    assert await adapter.has_collection("my_collection") is True
    mock_boto_client.get_index.assert_called_once_with(
        vectorBucketName="test-bucket", indexName="my-collection"
    )


@pytest.mark.asyncio
async def test_has_collection_false(adapter, mock_boto_client):
    mock_boto_client.get_index.side_effect = _not_found_error("GetIndex")
    assert await adapter.has_collection("my_collection") is False


@pytest.mark.asyncio
async def test_create_collection_creates_bucket_and_index(adapter, mock_boto_client):
    mock_boto_client.get_index.side_effect = _not_found_error("GetIndex")

    await adapter.create_collection("my_collection")

    mock_boto_client.create_vector_bucket.assert_called_once_with(vectorBucketName="test-bucket")
    mock_boto_client.create_index.assert_called_once_with(
        vectorBucketName="test-bucket",
        indexName="my-collection",
        dataType="float32",
        dimension=4,
        distanceMetric="cosine",
    )


@pytest.mark.asyncio
async def test_create_collection_skips_if_exists(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}

    await adapter.create_collection("my_collection")

    mock_boto_client.create_index.assert_not_called()


@pytest.mark.asyncio
async def test_create_data_points_writes_vectors(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}

    data_points = [
        make_data_point("11111111-1111-1111-1111-111111111111", "hello"),
        make_data_point("22222222-2222-2222-2222-222222222222", "world"),
    ]
    await adapter.create_data_points("my_collection", data_points)

    mock_boto_client.put_vectors.assert_called_once()
    call_kwargs = mock_boto_client.put_vectors.call_args.kwargs
    assert call_kwargs["vectorBucketName"] == "test-bucket"
    assert call_kwargs["indexName"] == "my-collection"
    assert len(call_kwargs["vectors"]) == 2
    assert call_kwargs["vectors"][0]["key"] == "11111111-1111-1111-1111-111111111111"
    assert call_kwargs["vectors"][0]["data"]["float32"] == [0.1, 0.1, 0.1, 0.1]
    assert call_kwargs["vectors"][0]["metadata"]["text"] == "hello"
    assert isinstance(call_kwargs["vectors"][0]["metadata"]["payload"], str)


@pytest.mark.asyncio
async def test_create_data_points_batches_large_writes(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}

    data_points = [make_data_point(str(uuid.uuid4()), f"item{i}") for i in range(750)]
    await adapter.create_data_points("my_collection", data_points)

    assert mock_boto_client.put_vectors.call_count == 2
    first_batch = mock_boto_client.put_vectors.call_args_list[0].kwargs["vectors"]
    second_batch = mock_boto_client.put_vectors.call_args_list[1].kwargs["vectors"]
    assert len(first_batch) == 500
    assert len(second_batch) == 250


@pytest.mark.asyncio
async def test_search_by_text(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    mock_boto_client.query_vectors.return_value = {
        "vectors": [
            {
                "key": "11111111-1111-1111-1111-111111111111",
                "distance": 0.05,
                "metadata": {"payload": json.dumps({"foo": "bar"})},
            }
        ]
    }

    results = await adapter.search(collection_name="my_collection", query_text="hello", limit=5)

    assert len(results) == 1
    assert results[0].score == 0.05
    assert results[0].payload["foo"] == "bar"
    call_kwargs = mock_boto_client.query_vectors.call_args.kwargs
    assert call_kwargs["topK"] == 5
    assert "filter" not in call_kwargs


@pytest.mark.asyncio
async def test_search_missing_collection_returns_empty(adapter, mock_boto_client):
    mock_boto_client.get_index.side_effect = _not_found_error("GetIndex")

    results = await adapter.search(collection_name="my_collection", query_text="hello")

    assert results == []
    mock_boto_client.query_vectors.assert_not_called()


@pytest.mark.asyncio
async def test_search_requires_text_or_vector(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}

    with pytest.raises(MissingQueryParameterError):
        await adapter.search(collection_name="my_collection")


@pytest.mark.asyncio
async def test_search_with_node_name_or_filter(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    mock_boto_client.query_vectors.return_value = {"vectors": []}

    await adapter.search(
        collection_name="my_collection",
        query_text="hello",
        node_name=["setA", "setB"],
    )

    call_kwargs = mock_boto_client.query_vectors.call_args.kwargs
    assert call_kwargs["filter"] == {"belongs_to_set": {"$in": ["setA", "setB"]}}


@pytest.mark.asyncio
async def test_search_with_node_name_and_filter(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    mock_boto_client.query_vectors.return_value = {"vectors": []}

    await adapter.search(
        collection_name="my_collection",
        query_text="hello",
        node_name=["setA", "setB"],
        node_name_filter_operator="AND",
    )

    call_kwargs = mock_boto_client.query_vectors.call_args.kwargs
    assert call_kwargs["filter"] == {
        "$and": [
            {"belongs_to_set": {"$eq": "setA"}},
            {"belongs_to_set": {"$eq": "setB"}},
        ]
    }


@pytest.mark.asyncio
async def test_search_caps_topk_at_10000(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    mock_boto_client.query_vectors.return_value = {"vectors": []}

    await adapter.search(collection_name="my_collection", query_text="hello", limit=50000)

    call_kwargs = mock_boto_client.query_vectors.call_args.kwargs
    assert call_kwargs["topK"] == 10000


@pytest.mark.asyncio
async def test_retrieve_returns_payload(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}
    mock_boto_client.get_vectors.return_value = {
        "vectors": [
            {
                "key": "11111111-1111-1111-1111-111111111111",
                "metadata": {"payload": json.dumps({"x": 1})},
            }
        ]
    }

    results = await adapter.retrieve("my_collection", ["11111111-1111-1111-1111-111111111111"])

    assert len(results) == 1
    assert results[0].payload["x"] == 1
    assert str(results[0].payload["id"]) == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_delete_data_points(adapter, mock_boto_client):
    mock_boto_client.get_index.return_value = {"index": {}}

    await adapter.delete_data_points(
        "my_collection",
        ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
    )

    mock_boto_client.delete_vectors.assert_called_once_with(
        vectorBucketName="test-bucket",
        indexName="my-collection",
        keys=["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"],
    )


@pytest.mark.asyncio
async def test_prune_deletes_all_indexes(adapter, mock_boto_client):
    mock_boto_client.list_indexes.return_value = {
        "indexes": [{"indexName": "idx-a"}, {"indexName": "idx-b"}],
        "nextToken": None,
    }

    await adapter.prune()

    assert mock_boto_client.delete_index.call_count == 2
    mock_boto_client.delete_index.assert_any_call(vectorBucketName="test-bucket", indexName="idx-a")
    mock_boto_client.delete_index.assert_any_call(vectorBucketName="test-bucket", indexName="idx-b")
