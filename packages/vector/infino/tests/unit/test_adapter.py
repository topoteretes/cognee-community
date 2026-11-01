"""Behavioral tests against the real Infino engine.

Infino is embedded (the pip package is the engine), so unlike server-backed
adapters this tier exercises real storage and real vector search offline: a
temporary directory, no network, no secrets.
"""

import asyncio
import tempfile
import uuid

from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_infino import InfinoAdapter

DIM = 16  # the engine's minimum vector dimensionality


class FakeEmbedder:
    """Deterministic: hashes each text onto one of DIM one-hot vectors."""

    async def embed_text(self, data):
        out = []
        for text in data:
            vector = [0.0] * DIM
            vector[hash(text) % DIM] = 1.0
            out.append(vector)
        return out

    def get_vector_size(self):
        return DIM

    def get_batch_size(self):
        return 128


class Doc(DataPoint):
    text: str
    topic: str = ""
    metadata: dict = {"index_fields": ["text"]}


def docs():
    return [
        Doc(id=uuid.uuid4(), text="cancel subscription billing", topic="billing"),
        Doc(id=uuid.uuid4(), text="dark mode appearance", topic="ui", belongs_to_set=["set-a"]),
        Doc(
            id=uuid.uuid4(),
            text="refund to original payment",
            topic="billing",
            belongs_to_set=["set-a", "set-b"],
        ),
    ]


def run(coroutine):
    return asyncio.run(coroutine)


def test_collections_are_created_idempotently_and_listed():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            assert not await adapter.has_collection("notes")
            await adapter.create_collection("notes")
            await adapter.create_collection("notes")  # second create is a no-op
            assert await adapter.has_collection("notes")
            assert await adapter.get_collection_names() == ["notes"]

    run(scenario())


def test_create_data_points_upserts_rather_than_duplicating():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_collection("notes")
            points = docs()
            await adapter.create_data_points("notes", points)
            await adapter.create_data_points("notes", points[:1])  # re-add one
            everything = await adapter.search("notes", query_text="anything", limit=None)
            assert len(everything) == 3

    run(scenario())


def test_search_returns_distance_ordered_payloads():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_collection("notes")
            await adapter.create_data_points("notes", docs())

            hits = await adapter.search(
                "notes", query_text="cancel subscription billing", limit=2
            )
            assert len(hits) == 2
            assert hits[0].payload["text"] == "cancel subscription billing"
            assert hits[0].score <= hits[1].score, "lower score is better"

            vector = (await FakeEmbedder().embed_text(["dark mode appearance"]))[0]
            by_vector = await adapter.search("notes", query_vector=vector, limit=1)
            assert by_vector[0].payload["topic"] == "ui"

    run(scenario())


def test_node_name_filters_or_and_and():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_collection("notes")
            await adapter.create_data_points("notes", docs())

            any_of = await adapter.search(
                "notes", query_text="anything", limit=3, node_name=["set-a"]
            )
            assert len(any_of) == 2

            all_of = await adapter.search(
                "notes",
                query_text="anything",
                limit=3,
                node_name=["set-a", "set-b"],
                node_name_filter_operator="AND",
            )
            assert len(all_of) == 1
            assert all_of[0].payload["text"].startswith("refund")

    run(scenario())


def test_search_on_a_missing_collection_is_empty_not_an_error():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            assert await adapter.search("nowhere", query_text="x", limit=1) == []

    run(scenario())


def test_retrieve_delete_and_prune():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_collection("notes")
            points = docs()
            await adapter.create_data_points("notes", points)

            got = await adapter.retrieve("notes", [str(points[0].id)])
            assert len(got) == 1
            assert got[0].payload["topic"] == "billing"

            await adapter.delete_data_points("notes", [points[0].id])
            left = await adapter.search("notes", query_text="anything", limit=None)
            assert len(left) == 2

            await adapter.prune()
            assert await adapter.get_collection_names() == []

    run(scenario())


def test_index_data_points_lands_in_the_derived_collection():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_vector_index("DocumentChunk", "text")
            await adapter.index_data_points("DocumentChunk", "text", docs())
            assert await adapter.has_collection("DocumentChunk_text")
            hits = await adapter.search("DocumentChunk_text", query_text="dark mode", limit=1)
            assert len(hits) == 1

    run(scenario())


def test_batch_search_answers_each_query():
    async def scenario():
        with tempfile.TemporaryDirectory() as root:
            adapter = InfinoAdapter(root, None, FakeEmbedder())
            await adapter.create_collection("notes")
            await adapter.create_data_points("notes", docs())
            batches = await adapter.batch_search(
                "notes", ["billing refund", "dark mode"], limit=2
            )
            assert len(batches) == 2
            assert all(len(batch) == 2 for batch in batches)

    run(scenario())
