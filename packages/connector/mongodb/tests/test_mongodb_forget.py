"""Forget-on-delete must remove a document's cognified graph content, not just its Data record.

Ingests two Mongo documents, cognifies (LLM + embeddings mocked, so no API key and
no network), deletes one document upstream, re-syncs, and asserts the deleted
document's extracted entity is gone from the graph while the surviving document's
entity remains. This is the acceptance criterion the connector's own unit tests
cannot reach: ``orphan_cleanup`` is what turns dlt's hard-delete marker into a
removal from the graph and vector stores.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_mongodb import mongodb_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "mongodb_forget_test"
# Distinctive tokens so each document maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


class FakeMongoCollection:
    """In-memory stand-in for a pymongo collection.

    Supports only the shapes the connector issues: an equality base filter, a
    ``{"$gt": value}`` cursor term, and an ``{"_id": {"$in": [...]}}`` term.
    """

    def __init__(self, documents=()):
        self.documents = [dict(document) for document in documents]

    def find(self, query_filter=None, projection=None, hint=None):
        for document in self.documents:
            if not self._matches(document, query_filter or {}):
                continue
            if projection == {"_id": 1}:
                yield {"_id": document["_id"]}
            elif projection:
                yield {key: document[key] for key in projection if key in document}
            else:
                yield dict(document)

    @staticmethod
    def _matches(document, query_filter):
        for key, condition in query_filter.items():
            if isinstance(condition, dict):
                if "$gt" in condition:
                    value = document.get(key)
                    if value is None or not value > condition["$gt"]:
                        return False
                if "$in" in condition and document.get(key) not in condition["$in"]:
                    return False
            elif document.get(key) != condition:
                return False
        return True


class _FakeDatabase:
    """Stands in for a pymongo Database: ``db[collection]``."""

    def __init__(self, collection):
        self._collection = collection

    def __getitem__(self, _name):
        return self._collection


class FakeMongoClient:
    """Stands in for a pymongo MongoClient: ``client[database][collection]``."""

    def __init__(self, collection):
        self._database = _FakeDatabase(collection)

    def __getitem__(self, _name):
        return self._database


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    for _node_id, properties in nodes:
        if any(token in str(value).lower() for value in (properties or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def _source(collection):
    return mongodb_source(
        database="testdb",
        collection="tickets",
        client=FakeMongoClient(collection),
        text_fields=["subject", "body"],
        title_field="subject",
    )


async def _sync(collection):
    await cognee.add(
        _source(collection),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_document_forgets_its_graph_content(clean_environment):
    collection = FakeMongoCollection(
        [
            {
                "_id": "a",
                "updatedAt": 1,
                "subject": f"{ALPHA} login",
                "body": f"{ALPHA} is a company in the logistics sector.",
            },
            {
                "_id": "b",
                "updatedAt": 2,
                "subject": f"{BRAVO} billing",
                "body": f"{BRAVO} is an unrelated company in the finance sector.",
            },
        ]
    )

    await _sync(collection)
    assert await _graph_has(ALPHA), "document a's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "document b's entity should be in the graph after ingest"

    # Delete document b upstream; the next sync must forget its content.
    collection.documents = [document for document in collection.documents if document["_id"] != "b"]
    await _sync(collection)

    assert await _graph_has(ALPHA), "surviving document a must remain after deleting b"
    assert not await _graph_has(BRAVO), (
        "deleted document b's entity must be removed from the graph (forget-on-delete), "
        "not just its Data record"
    )


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_reingest(clean_environment):
    """A no-op re-sync must leave the graph alone: only changed documents re-enter."""
    collection = FakeMongoCollection(
        [{"_id": "a", "updatedAt": 1, "subject": f"{ALPHA} login", "body": f"{ALPHA} logistics."}]
    )

    await _sync(collection)
    nodes_before, _ = await (await get_graph_engine()).get_graph_data()
    count_before = len(nodes_before)

    await _sync(collection)

    nodes_after, _ = await (await get_graph_engine()).get_graph_data()
    assert len(nodes_after) == count_before, (
        "an unchanged re-sync must not add nodes; the content-hash data_id has to be stable"
    )
