"""Adapter hooks against a live Elasticsearch cluster, without an LLM or embedding provider.

The unit tier runs these paths against an in-memory fake, which cannot prove the
painless detag script, script_score, msearch or the real update and kNN semantics.
This script calls every hook cognee uses on a real cluster, with a deterministic
stub embedder and two datasets sharing the indices. Needs only a reachable cluster
(VECTOR_DB_URL, default http://localhost:9200; optionally VECTOR_DB_KEY). CI runs it
in .github/workflows/test_elasticsearch.yml against 8.x and 9.x.
"""

import asyncio
import os
import uuid

from cognee_community_vector_adapter_elasticsearch import ElasticsearchAdapter

DIMS = 8


def unit(axis: int, sign: float = 1.0) -> list[float]:
    vector = [0.0] * DIMS
    vector[axis] = sign
    return vector


E0, E1, NEG_E0 = unit(0), unit(1), unit(0, -1.0)


class StubEmbeddings:
    """Known texts map to fixed vectors, so distances are exact and predictable."""

    vectors = {"zero": E0, "one": E1}

    async def embed_text(self, texts):
        return [self.vectors.get(text, unit(2)) for text in texts]

    def get_vector_size(self):
        return DIMS

    def get_batch_size(self):
        return 16


def adapter(database_name: str, prefix: str) -> ElasticsearchAdapter:
    return ElasticsearchAdapter(
        url=os.getenv("VECTOR_DB_URL", "http://localhost:9200"),
        api_key=os.getenv("VECTOR_DB_KEY") or None,
        embedding_engine=StubEmbeddings(),
        database_name=database_name,
        index_prefix=prefix,
    )


def point(point_id: str, vector: list[float], text: str, tags: list[str]) -> dict:
    return {
        "id": point_id,
        "vector": vector,
        "payload": {"id": point_id, "text": text, "belongs_to_set": tags},
    }


def close(actual: float, expected: float, tolerance: float) -> bool:
    return abs(actual - expected) <= tolerance


async def main():
    # A fresh prefix per run, so a shared or local cluster is never touched outside it.
    prefix = f"cognee-live-{uuid.uuid4().hex[:8]}"
    a, b = adapter("dataset-a", prefix), adapter("dataset-b", prefix)
    collection = "Entity_name"
    r1, r2, r3 = (str(uuid.uuid4()) for _ in range(3))

    try:
        # Same id in both datasets, different payloads: each dataset sees only its own.
        await a.upsert_raw_vectors(
            collection,
            [
                point(r1, E0, "a-r1", ["A", "B"]),
                point(r2, E1, "a-r2", ["B"]),
                point(r3, NEG_E0, "a-r3", ["A"]),
            ],
        )
        await b.upsert_raw_vectors(collection, [point(r1, E1, "b-r1", ["A"])])

        (a_r1,) = await a.retrieve(collection, [r1])
        (b_r1,) = await b.retrieve(collection, [r1])
        assert (a_r1.payload["text"], b_r1.payload["text"]) == ("a-r1", "b-r1")
        assert {str(r.id) for r in await b.retrieve(collection, [r1, r2, r3])} == {r1}
        assert await a.get_collection_names() == [collection]

        # retrieve(include_vector=True) returns the stored vector, also on 9.x where
        # new indices exclude vectors from _source by default.
        (with_vector,) = await a.retrieve(collection, [r1], include_vector=True)
        assert with_vector.payload["vector"] == E0, with_vector.payload["vector"]

        # kNN search: cosine distances, ascending, scoped to the dataset. The index
        # uses the server's default quantized index_options (int8_hnsw on 8.x), which
        # on these tiny unit vectors squashes the opposite vector to ~1.0 instead of
        # 2.0, so only the order and the identical vector's distance are checked
        # here; score_by_ids below checks exact distances.
        hits = await a.search(collection, query_vector=E0, limit=10, include_payload=True)
        assert [str(h.id) for h in hits] == [r1, r2, r3], hits
        assert close(hits[0].score, 0.0, 0.02), hits[0].score
        assert all(0.0 <= h.score <= 2.0 + 1e-6 for h in hits), hits
        and_hits = await a.search(
            collection,
            query_vector=E0,
            limit=10,
            node_name=["A", "B"],
            node_name_filter_operator="AND",
        )
        assert [str(h.id) for h in and_hits] == [r1]

        # batch_search: one msearch round trip, one list per query.
        zero, one = await a.batch_search(collection, ["zero", "one"], limit=1)
        assert [str(h.id) for h in zero] == [r1] and [str(h.id) for h in one] == [r2]

        # score_by_ids: exact cosine distance of the requested rows only.
        scored = {
            str(r.id): r.score
            for r in await a.score_by_ids(collection, [r1, r2, r3, str(uuid.uuid4())], E0)
        }
        assert set(scored) == {r1, r2, r3}, scored
        for row, expected in ((r1, 0.0), (r2, 1.0), (r3, 2.0)):
            assert close(scored[row], expected, 1e-5), (row, scored)

        # update_payload changes the field, keeps the vector, refuses unknown fields.
        await a.update_payload(collection, {r1: {"text": "a-r1-updated"}})
        (updated,) = await a.retrieve(collection, [r1], include_vector=True)
        assert updated.payload["text"] == "a-r1-updated"
        assert updated.payload["vector"] == E0
        try:
            await a.update_payload(collection, {r1: {"no_such_field": 1}})
        except ValueError:
            pass
        else:
            raise AssertionError("update_payload accepted a field missing from the payload")

        # Re-indexing an id unions its tags with the stored ones.
        await a.upsert_raw_vectors(collection, [point(r3, NEG_E0, "a-r3", ["C"])])
        (merged,) = await a.retrieve(collection, [r3])
        assert merged.payload["belongs_to_set"] == ["A", "C"], merged.payload

        # The painless detag script: strips the tag, deletes rows left with none,
        # never touches the other dataset.
        await a.remove_belongs_to_set_tags(["B"])
        rows = {str(r.id): r.payload for r in await a.retrieve(collection, [r1, r2])}
        assert set(rows) == {r1}, rows
        assert rows[r1]["belongs_to_set"] == ["A"]
        (detag_hit,) = await a.search(collection, query_vector=E0, limit=1)
        assert str(detag_hit.id) == r1 and close(detag_hit.score, 0.0, 0.02)
        # Scoped to node ids: only r3 loses "C".
        await a.remove_belongs_to_set_tags(["A", "C"], node_ids=[r3])
        assert await a.retrieve(collection, [r3]) == []
        (b_r1,) = await b.retrieve(collection, [r1])
        assert b_r1.payload["belongs_to_set"] == ["A"]

        # delete_data_points removes this dataset's copy only.
        await a.delete_data_points(collection, [r1])
        assert await a.retrieve(collection, [r1]) == []
        assert [str(r.id) for r in await b.retrieve(collection, [r1])] == [r1]

        # prune drops this dataset's rows and keeps the index while another dataset uses it.
        await a.upsert_raw_vectors(collection, [point(r2, E1, "a-r2", ["B"])])
        await a.prune()
        assert await a.retrieve(collection, [r2]) == []
        assert await a.get_collection_names() == []
        assert await b.has_collection(collection)

        # delete_collection drops the index, whichever datasets still hold rows.
        await b.delete_collection(collection)
        assert not await b.has_collection(collection)
        await b.delete_collection(collection)  # a missing collection is not an error

        print("Elasticsearch live adapter checks passed")
    finally:
        for engine in (a, b):
            for index in await engine._managed_indices():
                await engine.client.indices.delete(index=index, ignore_unavailable=True)
            await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
