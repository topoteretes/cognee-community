"""Offline functional tests for the S3 Vectors adapter.

A fake boto3 client stands in for the real ``s3vectors`` client, so the tests
cover batching, filter building, score mapping, payload round-trips and error
handling without an AWS account.
"""

import json

import pytest
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee_community_vector_adapter_s3vectors import s3vectors_adapter as adapter_module
from cognee_community_vector_adapter_s3vectors.s3vectors_adapter import S3VectorsAdapter
from contract_suite import FakeEmbeddingEngine


class FakeS3VectorsClient:
    """Records every call and serves scripted responses."""

    class exceptions:  # mirrors how boto3 exposes modeled service errors
        class NotFoundException(Exception):
            pass

        class ConflictException(Exception):
            pass

    def __init__(self, indexes=None, query_responses=None, list_index_responses=None):
        self.calls = []
        self.buckets = set()
        self.indexes = set(indexes or [])
        self.vectors = {}
        self.query_responses = list(query_responses or [])
        self.list_index_responses = list(list_index_responses or [])
        self.conflict_on_create_index = False

    # -- helpers ----------------------------------------------------------
    def call_args(self, operation):
        return [kwargs for name, kwargs in self.calls if name == operation]

    def _record(self, operation, kwargs):
        self.calls.append((operation, kwargs))

    # -- bucket operations -------------------------------------------------
    def get_vector_bucket(self, **kwargs):
        self._record("get_vector_bucket", kwargs)
        if kwargs["vectorBucketName"] not in self.buckets:
            raise self.exceptions.NotFoundException("bucket not found")
        return {"vectorBucketName": kwargs["vectorBucketName"]}

    def create_vector_bucket(self, **kwargs):
        self._record("create_vector_bucket", kwargs)
        if kwargs["vectorBucketName"] in self.buckets:
            raise self.exceptions.ConflictException("bucket exists")
        self.buckets.add(kwargs["vectorBucketName"])
        return {}

    # -- index operations --------------------------------------------------
    def get_index(self, **kwargs):
        self._record("get_index", kwargs)
        if kwargs["indexName"] not in self.indexes:
            raise self.exceptions.NotFoundException("index not found")
        return {"indexName": kwargs["indexName"]}

    def create_index(self, **kwargs):
        self._record("create_index", kwargs)
        if self.conflict_on_create_index:
            raise self.exceptions.ConflictException("index exists")
        self.indexes.add(kwargs["indexName"])
        return {}

    def list_indexes(self, **kwargs):
        self._record("list_indexes", kwargs)
        if kwargs["vectorBucketName"] not in self.buckets:
            raise self.exceptions.NotFoundException("bucket not found")
        if self.list_index_responses:
            return self.list_index_responses.pop(0)
        return {"indexes": [{"indexName": name} for name in sorted(self.indexes)]}

    def delete_index(self, **kwargs):
        self._record("delete_index", kwargs)
        self.indexes.discard(kwargs["indexName"])
        return {}

    # -- vector operations -------------------------------------------------
    def put_vectors(self, **kwargs):
        self._record("put_vectors", kwargs)
        store = self.vectors.setdefault(kwargs["indexName"], {})
        for vector in kwargs["vectors"]:
            store[vector["key"]] = vector
        return {}

    def get_vectors(self, **kwargs):
        self._record("get_vectors", kwargs)
        store = self.vectors.get(kwargs["indexName"], {})
        return {
            "vectors": [
                {"key": key, "metadata": store[key].get("metadata")}
                for key in kwargs["keys"]
                if key in store
            ]
        }

    def delete_vectors(self, **kwargs):
        self._record("delete_vectors", kwargs)
        store = self.vectors.get(kwargs["indexName"], {})
        for key in kwargs["keys"]:
            store.pop(key, None)
        return {}

    def query_vectors(self, **kwargs):
        self._record("query_vectors", kwargs)
        if kwargs["indexName"] not in self.indexes:
            raise self.exceptions.NotFoundException("index not found")
        if self.query_responses:
            return self.query_responses.pop(0)
        return {"vectors": []}


class SamplePoint(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}


def make_adapter(client, **overrides):
    adapter = S3VectorsAdapter(
        embedding_engine=FakeEmbeddingEngine(),
        vector_bucket_name="test-bucket",
        **overrides,
    )
    adapter.client = client
    return adapter


def uuid_key(index: int) -> str:
    """Real cognee vector keys are UUID strings; ScoredResult.id validates them."""
    return f"00000000-0000-0000-0000-{index:012d}"


def query_result(key, distance, payload=None):
    result = {"key": key, "distance": distance}
    if payload is not None:
        result["metadata"] = {"payload": json.dumps(payload)}
    return result


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def test_sanitize_name_follows_s3_vectors_rules():
    sanitize = S3VectorsAdapter._sanitize_name

    assert sanitize("DocumentChunk_text") == "documentchunk-text"
    assert sanitize("--Node--Set--") == "node-set"
    # Too-short and empty names are padded/defaulted to the 3-char minimum.
    assert sanitize("_x") == "idx-x"
    assert sanitize("!!!") == "cognee-vectors"
    assert len(sanitize("a" * 100)) == 63


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_endpoint_url_is_kept_only_when_it_is_a_url():
    endpoint = "https://vpce-0abc.s3vectors.us-east-1.vpce.amazonaws.com"

    url_adapter = make_adapter(FakeS3VectorsClient(), url=endpoint)
    assert url_adapter.endpoint_url == endpoint

    # cognee fills VECTOR_DB_URL with the default LanceDB path when unset; a
    # filesystem path must not reach boto3 as an endpoint.
    path_adapter = make_adapter(FakeS3VectorsClient(), url="C:\\cognee\\databases\\cognee.lancedb")
    assert path_adapter.endpoint_url is None


def test_incomplete_credentials_raise():
    with pytest.raises(ValueError):
        make_adapter(FakeS3VectorsClient(), vector_db_username="akid-only")

    with pytest.raises(ValueError):
        make_adapter(FakeS3VectorsClient(), api_key="secret-only")


def test_complete_credentials_are_accepted():
    adapter = make_adapter(
        FakeS3VectorsClient(), vector_db_username="akid", vector_db_password="secret"
    )

    assert adapter.client is not None


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


async def test_create_collection_creates_bucket_and_cosine_index():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    await adapter.create_collection("DocumentChunk_text")

    assert client.call_args("create_vector_bucket") == [{"vectorBucketName": "test-bucket"}]
    assert client.call_args("create_index") == [
        {
            "vectorBucketName": "test-bucket",
            "indexName": "documentchunk-text",
            "dataType": "float32",
            "dimension": 8,
            "distanceMetric": "cosine",
            "metadataConfiguration": {"nonFilterableMetadataKeys": ["payload"]},
        }
    ]


async def test_create_collection_is_idempotent():
    client = FakeS3VectorsClient(indexes={"documentchunk-text"})
    adapter = make_adapter(client)

    await adapter.create_collection("DocumentChunk_text")

    assert client.call_args("create_index") == []


async def test_create_collection_tolerates_a_concurrent_create():
    client = FakeS3VectorsClient()
    client.conflict_on_create_index = True
    adapter = make_adapter(client)

    await adapter.create_collection("DocumentChunk_text")  # must not raise


async def test_has_collection_sanitizes_the_name():
    client = FakeS3VectorsClient(indexes={"documentchunk-text"})
    adapter = make_adapter(client)

    assert await adapter.has_collection("DocumentChunk_text") is True
    assert client.call_args("get_index")[0]["indexName"] == "documentchunk-text"
    assert await adapter.has_collection("Missing_text") is False


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def test_create_data_points_writes_payload_and_node_sets():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)
    point = SamplePoint(text="hello world", belongs_to_set=["node-set-a", "node-set-b"])

    await adapter.create_data_points("DocumentChunk_text", [point])

    put = client.call_args("put_vectors")[0]
    assert put["indexName"] == "documentchunk-text"
    vector = put["vectors"][0]
    assert vector["key"] == str(point.id)
    assert vector["data"]["float32"] == FakeEmbeddingEngine()._embed_one("hello world")
    assert vector["metadata"]["belongs_to_set"] == ["node-set-a", "node-set-b"]

    # Non-JSON property values (the UUID id) survive the JSON-string encoding.
    payload = json.loads(vector["metadata"]["payload"])
    assert payload["id"] == str(point.id)
    assert payload["text"] == "hello world"


async def test_create_data_points_batches_put_calls(monkeypatch):
    monkeypatch.setattr(adapter_module, "PUT_BATCH_SIZE", 2)
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)
    points = [SamplePoint(text=f"text {i}") for i in range(5)]

    await adapter.create_data_points("DocumentChunk_text", points)

    puts = client.call_args("put_vectors")
    assert [len(put["vectors"]) for put in puts] == [2, 2, 1]


async def test_create_data_points_skips_empty_batches():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    await adapter.create_data_points("DocumentChunk_text", [])

    assert client.calls == []


# ---------------------------------------------------------------------------
# seeded store helper for read paths
# ---------------------------------------------------------------------------


def seeded_client(points_metadata):
    """Build a client with one existing index and stored vectors."""
    client = FakeS3VectorsClient(indexes={"documentchunk-text"})
    client.buckets.add("test-bucket")
    store = client.vectors.setdefault("documentchunk-text", {})
    for key, payload in points_metadata.items():
        store[key] = {"key": key, "metadata": {"payload": json.dumps(payload)}}
    return client


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_maps_cosine_distance_to_score():
    client = seeded_client({})
    client.query_responses = [
        {
            "vectors": [
                query_result(uuid_key(1), 0.25, {"text": "a"}),
                query_result(uuid_key(2), 0.75, {"text": "b"}),
            ]
        }
    ]
    adapter = make_adapter(client)

    results = await adapter.search(
        "DocumentChunk_text", query_vector=[0.1] * 8, limit=5, include_payload=True
    )

    # normalized=True (default) returns the raw cosine distance, lower is better.
    assert [result.score for result in results] == [0.25, 0.75]
    assert results[0].payload["id"] == parse_id(uuid_key(1))
    assert results[0].payload["text"] == "a"
    request = client.call_args("query_vectors")[0]
    assert request["topK"] == 5
    assert request["returnDistance"] is True
    assert request["returnMetadata"] is True


async def test_search_unnormalized_returns_cosine_similarity():
    client = seeded_client({})
    client.query_responses = [{"vectors": [query_result(uuid_key(1), 0.25, {"text": "a"})]}]
    adapter = make_adapter(client)

    results = await adapter.search(
        "DocumentChunk_text", query_vector=[0.1] * 8, normalized=False, include_payload=True
    )

    assert results[0].score == pytest.approx(0.75)


async def test_search_omits_payload_and_metadata_by_default():
    client = seeded_client({})
    client.query_responses = [{"vectors": [query_result(uuid_key(1), 0.25, {"text": "a"})]}]
    adapter = make_adapter(client)

    results = await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8)

    assert results[0].payload is None
    assert client.call_args("query_vectors")[0]["returnMetadata"] is False


async def test_search_embeds_query_text():
    client = seeded_client({})
    adapter = make_adapter(client)

    await adapter.search("DocumentChunk_text", query_text="some query", limit=3)

    request = client.call_args("query_vectors")[0]
    assert request["queryVector"]["float32"] == FakeEmbeddingEngine()._embed_one("some query")


async def test_search_builds_node_set_filters():
    client = seeded_client({})
    adapter = make_adapter(client)

    await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8, node_name=["set-a", "set-b"])
    assert client.call_args("query_vectors")[0]["filter"] == {
        "belongs_to_set": {"$in": ["set-a", "set-b"]}
    }

    await adapter.search(
        "DocumentChunk_text",
        query_vector=[0.1] * 8,
        node_name=["set-a", "set-b"],
        node_name_filter_operator="AND",
    )
    assert client.call_args("query_vectors")[1]["filter"] == {
        "$and": [
            {"belongs_to_set": {"$eq": "set-a"}},
            {"belongs_to_set": {"$eq": "set-b"}},
        ]
    }


async def test_search_without_filter_omits_the_filter_key():
    client = seeded_client({})
    adapter = make_adapter(client)

    await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8)

    assert "filter" not in client.call_args("query_vectors")[0]


async def test_search_limit_none_uses_the_service_maximum():
    client = seeded_client({})
    adapter = make_adapter(client)

    await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8, limit=None)

    assert client.call_args("query_vectors")[0]["topK"] == adapter_module.MAX_TOP_K


async def test_search_non_positive_limit_short_circuits():
    client = seeded_client({})
    adapter = make_adapter(client)

    assert await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8, limit=0) == []
    assert client.call_args("query_vectors") == []


async def test_search_requires_text_or_vector():
    client = seeded_client({})
    adapter = make_adapter(client)

    with pytest.raises(MissingQueryParameterError):
        await adapter.search("DocumentChunk_text")


async def test_search_missing_collection_raises():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    with pytest.raises(CollectionNotFoundError):
        await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8)


async def test_search_pages_through_query_results():
    first_page = [query_result(uuid_key(i), 0.1) for i in range(100)]
    client = seeded_client({})
    client.query_responses = [
        {"vectors": first_page, "nextToken": "page-2"},
        {"vectors": [query_result(uuid_key(100), 0.2), query_result(uuid_key(101), 0.3)]},
    ]
    adapter = make_adapter(client)

    results = await adapter.search("DocumentChunk_text", query_vector=[0.1] * 8, limit=150)

    assert len(results) == 102
    queries = client.call_args("query_vectors")
    assert queries[0]["topK"] == 150
    assert "nextToken" not in queries[0]
    assert queries[1]["nextToken"] == "page-2"


# ---------------------------------------------------------------------------
# retrieve / delete
# ---------------------------------------------------------------------------


async def test_retrieve_returns_payloads_without_scores():
    client = seeded_client({uuid_key(1): {"text": "a"}, uuid_key(2): {"text": "b"}})
    adapter = make_adapter(client)

    results = await adapter.retrieve("DocumentChunk_text", [uuid_key(1), uuid_key(2), uuid_key(99)])

    assert [result.payload["text"] for result in results] == ["a", "b"]
    assert [result.score for result in results] == [0, 0]
    assert client.call_args("get_vectors")[0]["returnMetadata"] is True


async def test_retrieve_batches_get_calls(monkeypatch):
    monkeypatch.setattr(adapter_module, "GET_BATCH_SIZE", 2)
    client = seeded_client({uuid_key(i): {"text": str(i)} for i in range(5)})
    adapter = make_adapter(client)

    await adapter.retrieve("DocumentChunk_text", [uuid_key(i) for i in range(5)])

    assert [len(call["keys"]) for call in client.call_args("get_vectors")] == [2, 2, 1]


async def test_retrieve_missing_collection_raises():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    with pytest.raises(CollectionNotFoundError):
        await adapter.retrieve("DocumentChunk_text", ["key-1"])


async def test_delete_data_points_batches_delete_calls(monkeypatch):
    monkeypatch.setattr(adapter_module, "DELETE_BATCH_SIZE", 2)
    client = seeded_client({f"key-{i}": {"text": str(i)} for i in range(5)})
    adapter = make_adapter(client)

    await adapter.delete_data_points("DocumentChunk_text", [f"key-{i}" for i in range(5)])

    assert [len(call["keys"]) for call in client.call_args("delete_vectors")] == [2, 2, 1]
    assert client.vectors["documentchunk-text"] == {}


async def test_delete_data_points_is_idempotent():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    await adapter.delete_data_points("DocumentChunk_text", ["key-1"])  # missing collection
    assert client.call_args("delete_vectors") == []

    client.indexes.add("documentchunk-text")
    await adapter.delete_data_points("DocumentChunk_text", [])  # empty ids
    assert client.call_args("delete_vectors") == []


# ---------------------------------------------------------------------------
# batch search / indexing / prune
# ---------------------------------------------------------------------------


async def test_batch_search_runs_one_search_per_query():
    client = seeded_client({})
    client.query_responses = [{"vectors": []}, {"vectors": []}]
    adapter = make_adapter(client)

    results = await adapter.batch_search("DocumentChunk_text", ["first", "second"], limit=None)

    assert len(results) == 2
    queries = client.call_args("query_vectors")
    assert all(query["topK"] == adapter_module.MAX_TOP_K for query in queries)
    # Searches run concurrently, so compare the embedded queries as a set.
    embedded = sorted(query["queryVector"]["float32"] for query in queries)
    engine = FakeEmbeddingEngine()
    assert embedded == sorted([engine._embed_one("first"), engine._embed_one("second")])


async def test_index_data_points_writes_index_schema_vectors():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)
    point = SamplePoint(text="index me", belongs_to_set=["set-a"])

    await adapter.create_vector_index("SamplePoint", "text")
    await adapter.index_data_points("SamplePoint", "text", [point])

    put = client.call_args("put_vectors")[-1]
    assert put["indexName"] == "samplepoint-text"
    assert put["vectors"][0]["key"] == str(point.id)
    assert put["vectors"][0]["metadata"]["belongs_to_set"] == ["set-a"]
    assert json.loads(put["vectors"][0]["metadata"]["payload"])["text"] == "index me"


async def test_prune_deletes_every_index():
    client = FakeS3VectorsClient(indexes={"one", "two"})
    client.buckets.add("test-bucket")
    client.list_index_responses = [
        {"indexes": [{"indexName": "one"}], "nextToken": "page-2"},
        {"indexes": [{"indexName": "two"}]},
    ]
    adapter = make_adapter(client)

    await adapter.prune()

    assert client.call_args("list_indexes")[1]["nextToken"] == "page-2"
    assert [call["indexName"] for call in client.call_args("delete_index")] == ["one", "two"]


async def test_prune_on_missing_bucket_is_a_no_op():
    client = FakeS3VectorsClient()
    adapter = make_adapter(client)

    await adapter.prune()

    assert client.call_args("delete_index") == []
