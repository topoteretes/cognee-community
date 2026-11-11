"""Cognee vector adapter backed by Infino.

Infino (https://github.com/infino-ai/infino) is an embedded retrieval engine:
BM25, vector, hybrid search and SQL over data stored as standard Apache
Parquet, on a local path or directly on object storage. There is no server to
run: the `infino` package is the engine, and `vector_db_url` names where the
data lives — a local directory or an `s3://` bucket prefix. That makes agent
memory durable files you can read back with any Parquet tool.

Layout: one Infino table per cognee collection. Each row stores the data
point id, its embeddable text (BM25-indexed), the full payload as JSON, the
`belongs_to_set` tags as JSON, and the embedding under a cosine vector index.

The Infino binding is synchronous; every call is dispatched through
``asyncio.to_thread`` so the adapter stays a well-behaved async citizen.
"""

import asyncio
import json
import threading
from typing import Any, List, Optional
from uuid import UUID

import infino
import pyarrow as pa
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger

logger = get_logger("InfinoAdapter")

# How far beyond `limit` to over-fetch when a `node_name` filter applies. The
# filter is evaluated on the returned rows, so the fetch has to leave room for
# rows the filter drops.
NODE_FILTER_OVERFETCH = 4


def _quote(value: str) -> str:
    """Quote a string for use inside a SQL string literal."""
    return value.replace("'", "''")


class IndexSchema(DataPoint):
    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class InfinoAdapter(VectorDBInterface):
    name = "Infino"
    url: str = None
    api_key: str = None

    def __init__(
        self,
        url: str,
        api_key: Optional[str],
        embedding_engine: EmbeddingEngine,
        database_name: str = "cognee_db",
    ):
        self.url = url
        self.api_key = api_key
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self._db = None
        self._db_lock = threading.Lock()
        self._tables = {}

    # -- connection ---------------------------------------------------------

    def _database(self):
        """The Infino catalog for this adapter, opened once. `url` is a local
        directory or an object-store URI; the engine runs in-process either
        way."""
        with self._db_lock:
            if self._db is None:
                self._db = infino.connect(self.url)
            return self._db

    def _open_table(self, collection_name: str):
        with self._db_lock:
            table = self._tables.get(collection_name)
        if table is not None:
            return table
        try:
            table = self._database().open_table(collection_name)
        except Exception as error:
            raise CollectionNotFoundError(
                message=f"Collection {collection_name} not found!"
            ) from error
        with self._db_lock:
            self._tables[collection_name] = table
        return table

    def _schema(self) -> pa.Schema:
        vector_size = self.embedding_engine.get_vector_size()
        return pa.schema(
            [
                pa.field("id", pa.utf8(), nullable=False),
                pa.field("text", pa.large_utf8(), nullable=False),
                pa.field("payload", pa.large_utf8(), nullable=False),
                pa.field("belongs_to_set", pa.large_utf8(), nullable=False),
                pa.field("embedding", pa.list_(pa.float32(), vector_size), nullable=False),
            ]
        )

    # -- collections --------------------------------------------------------

    async def has_collection(self, collection_name: str) -> bool:
        def check() -> bool:
            return collection_name in self._database().list_tables()

        return await asyncio.to_thread(check)

    async def create_collection(self, collection_name: str, payload_schema=None):
        def create():
            db = self._database()
            if collection_name in db.list_tables():
                return
            vector_size = self.embedding_engine.get_vector_size()
            db.create_table(
                collection_name,
                self._schema(),
                infino.IndexSpec()
                .fts("text")
                .vector("embedding", vector_size, "cosine"),
            )

        await asyncio.to_thread(create)

    async def get_collection_names(self) -> list[str]:
        return await asyncio.to_thread(lambda: list(self._database().list_tables()))

    # -- data points --------------------------------------------------------

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(message=f"Collection {collection_name} not found!")

        vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        rows = [
            {
                "id": str(data_point.id),
                "text": str(DataPoint.get_embeddable_data(data_point)),
                "payload": json.dumps(data_point.model_dump(), default=str),
                "belongs_to_set": json.dumps(
                    [str(tag) for tag in (data_point.belongs_to_set or [])]
                ),
                "embedding": vectors[index],
            }
            for index, data_point in enumerate(data_points)
        ]

        def write():
            table = self._open_table(collection_name)
            # Upsert semantics: replace any rows that carry the same ids, then
            # append. A repeated pipeline run must not duplicate data points.
            ids = ", ".join(f"'{_quote(row['id'])}'" for row in rows)
            try:
                table.delete(f"id IN ({ids})")
            except Exception:
                # Nothing to replace on a fresh collection.
                pass
            table.append(rows)

        await asyncio.to_thread(write)

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        def read():
            table = self._open_table(collection_name)
            ids = ", ".join(f"'{_quote(str(id))}'" for id in data_point_ids)
            hits = self._database().query_sql(
                f"SELECT id, payload FROM {collection_name} WHERE id IN ({ids})"
            )
            return list(
                zip(
                    hits.column("id").to_pylist(),
                    hits.column("payload").to_pylist(),
                )
            )

        records = await asyncio.to_thread(read)
        return [
            ScoredResult(id=parse_id(str(row_id)), score=0, payload=json.loads(payload))
            for row_id, payload in records
        ]

    # -- search -------------------------------------------------------------

    def _parse_hits(self, hits) -> list[tuple[str, str, str, float]]:
        """(id, payload, belongs_to_set, score) tuples from a search result."""
        return list(
            zip(
                hits.column("id").to_pylist(),
                hits.column("payload").to_pylist(),
                hits.column("belongs_to_set").to_pylist(),
                hits.column("score").to_pylist(),
            )
        )

    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[list[float]] = None,
        limit: Optional[int] = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list[ScoredResult]:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            return []

        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]

        fetch = limit if limit else 0
        if node_name and fetch:
            fetch = fetch * NODE_FILTER_OVERFETCH

        def run():
            table = self._open_table(collection_name)
            row_count = None
            requested = fetch
            if not requested:
                counted = self._database().query_sql(
                    f"SELECT count(*) AS n FROM {collection_name}"
                )
                row_count = counted.column("n").to_pylist()[0]
                requested = int(row_count)
            if requested == 0:
                return []
            return self._parse_hits(
                table.vector_search(
                    "embedding",
                    query_vector,
                    requested,
                    # `score` is the ranking distance (cosine: lower is
                    # better); the engine allows projecting it by name.
                    projection=["id", "payload", "belongs_to_set", "score"],
                )
            )

        try:
            raw = await asyncio.to_thread(run)
        except CollectionNotFoundError:
            return []
        except Exception as error:
            logger.error("Error in Infino search: %s", str(error), exc_info=True)
            return []

        wanted = set(str(name) for name in node_name) if node_name else None
        results = []
        for row_id, payload, tags, score in raw:
            if wanted is not None:
                tag_set = set(json.loads(tags or "[]"))
                if node_name_filter_operator == "AND":
                    if not wanted.issubset(tag_set):
                        continue
                elif not (wanted & tag_set):
                    continue
            parsed = json.loads(payload)
            results.append(
                ScoredResult(
                    id=parse_id(str(row_id)),
                    payload={**parsed, "id": str(row_id)},
                    # Infino reports cosine distance: lower is better, which is
                    # exactly the ScoredResult contract. Passed through raw.
                    score=float(score),
                )
            )
            if limit and len(results) >= limit:
                break
        return results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: Optional[int] = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ):
        vectors = await self.embed_data(query_texts)
        return await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                )
                for vector in vectors
            ]
        )

    # -- indexes (cognee's index x property naming) --------------------------

    async def create_vector_index(self, index_name: str, index_property_name: str):
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=data_point.id,
                    text=getattr(data_point, data_point.metadata["index_fields"][0]),
                    belongs_to_set=(data_point.belongs_to_set or []),
                )
                for data_point in data_points
            ],
        )

    # -- deletion -----------------------------------------------------------

    async def delete_data_points(self, collection_name: str, data_point_ids: list[UUID]):
        def remove():
            table = self._open_table(collection_name)
            ids = ", ".join(f"'{_quote(str(id))}'" for id in data_point_ids)
            return table.delete(f"id IN ({ids})")

        return await asyncio.to_thread(remove)

    async def prune(self):
        def drop_all():
            db = self._database()
            for name in list(db.list_tables()):
                db.drop_table(name)
            with self._db_lock:
                self._tables.clear()

        await asyncio.to_thread(drop_all)
