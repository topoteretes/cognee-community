"""Regression tests for DuckDBAdapter.search vector ranking.

These pin two properties of the vector search that the adapter previously got wrong:

1. Results are ordered by ascending cosine distance (nearest match first). Before the fix the
   SQL had a ``LIMIT`` without an ``ORDER BY``, so it returned an arbitrary slice of rows rather
   than the closest ones.
2. ``ScoredResult.score`` is the raw cosine distance (lower is better), matching the built-in
   cognee adapters (LanceDB, PGVector) and the ``ScoredResult`` contract. Before the fix the
   adapter returned ``1 - distance``, which inverts the ranking for any consumer that sorts by
   score ascending.

The test runs entirely in-process against DuckDB with a stub embedding engine, so it needs no
network access or LLM provider.
"""

import asyncio
from uuid import uuid4

from cognee_community_hybrid_adapter_duckdb.duckdb_adapter import DuckDBAdapter, DuckDBDataPoint


class _StubEmbeddingEngine:
    """Return fixed vectors keyed by the text of each data point (no network)."""

    def __init__(self, text_to_vector: dict[str, list[float]], vector_size: int):
        self._text_to_vector = text_to_vector
        self._vector_size = vector_size

    async def embed_text(self, text: list[str]) -> list[list[float]]:
        return [self._text_to_vector[value] for value in text]

    def get_vector_size(self) -> int:
        return self._vector_size

    def get_batch_size(self) -> int:
        return 32


async def _seed_adapter() -> tuple[DuckDBAdapter, dict[str, str]]:
    # Query vector is [1, 0, 0]; cosine distances: near 0.0 < mid ~0.293 < far 1.0.
    text_to_vector = {
        "near": [1.0, 0.0, 0.0],
        "mid": [1.0, 1.0, 0.0],
        "far": [0.0, 0.0, 1.0],
    }
    adapter = DuckDBAdapter(embedding_engine=_StubEmbeddingEngine(text_to_vector, 3))

    points = {name: DuckDBDataPoint(id=uuid4(), text=name) for name in text_to_vector}
    ids = {name: str(point.id) for name, point in points.items()}

    await adapter.create_collection("points")
    # Insert in *reverse distance order* (far, mid, near) on purpose: an unordered ``LIMIT`` would
    # return rows in insertion order, so this ordering makes a missing ``ORDER BY`` fail the test.
    await adapter.create_data_points("points", [points["far"], points["mid"], points["near"]])
    return adapter, ids


async def test_search_orders_by_distance_and_returns_nearest_first():
    adapter, ids = await _seed_adapter()
    try:
        # limit=2 on 3 points: only meaningful if the two *nearest* rows are returned in order.
        results = await adapter.search(
            collection_name="points", query_vector=[1.0, 0.0, 0.0], limit=2
        )
    finally:
        await adapter.close()

    returned_ids = [str(result.id) for result in results]
    assert returned_ids == [ids["near"], ids["mid"]], (
        f"Expected the two nearest points in order, got {returned_ids}"
    )


async def test_score_is_raw_distance_lower_is_better():
    adapter, ids = await _seed_adapter()
    try:
        results = await adapter.search(
            collection_name="points", query_vector=[1.0, 0.0, 0.0], limit=3
        )
    finally:
        await adapter.close()

    # Look scores up by id so this assertion does not depend on result ordering: score is the raw
    # cosine distance (lower is better), not `1 - distance`. The old code scored the identical
    # vector ~1.0 instead of ~0.0.
    score_by_id = {str(result.id): result.score for result in results}
    assert score_by_id[ids["near"]] < 1e-6, (
        f"Nearest match should have distance ~0.0, got {score_by_id[ids['near']]}"
    )
    assert score_by_id[ids["far"]] > 0.99, (
        f"Farthest match should have distance ~1.0, got {score_by_id[ids['far']]}"
    )
    assert score_by_id[ids["near"]] < score_by_id[ids["mid"]] < score_by_id[ids["far"]], (
        f"Scores must increase with distance: {score_by_id}"
    )


async def _main() -> None:
    for test in (
        test_search_orders_by_distance_and_returns_nearest_first,
        test_score_is_raw_distance_lower_is_better,
    ):
        await test()
        print(f"PASSED: {test.__name__}")


if __name__ == "__main__":
    asyncio.run(_main())
