import asyncio
import os
from typing import Any, List, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

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
from qdrant_client import AsyncQdrantClient, models

from .quantization import (
    build_quantization_config,
    build_search_params,
)

logger = get_logger("QDrantAdapter")

DEFAULT_QDRANT_PORT = 6333


def serialize_for_json(obj: Any) -> Any:
    """Recursively convert UUIDs (and containers of them) to JSON-serializable
    values so returned payloads can be json.dumps()'d by cognee core (e.g. when
    logging search history)."""
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {key: serialize_for_json(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    return obj


class IndexSchema(DataPoint):
    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class QDrantAdapter(VectorDBInterface):
    name = "Qdrant"
    url: str = None
    api_key: str = None
    qdrant_path: str = None

    def __init__(
        self,
        url,
        api_key,
        embedding_engine: EmbeddingEngine,
        qdrant_path=None,
        database_name: str = "cognee_db",
        timeout: int = 120,
        vector_db_host: str = "",
        vector_db_port: str = "",
        **kwargs,
    ):
        # cognee >= 1.5.0 always passes vector_db_host / vector_db_port /
        # vector_db_username / vector_db_password to registered adapters. Host
        # and port are honored as a fallback when no VECTOR_DB_URL is set;
        # Qdrant authenticates with an API key, so the username/password pair
        # lands in **kwargs and is ignored.
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self.timeout = timeout

        if qdrant_path is not None:
            self.qdrant_path = qdrant_path
        else:
            if not url and vector_db_host:
                url = f"http://{vector_db_host}:{vector_db_port or DEFAULT_QDRANT_PORT}"
            self.url = url
            self.api_key = api_key
        self.VECTOR_DB_LOCK = asyncio.Lock()

        if os.getenv("QDRANT_QUANTIZATION", "none").lower().startswith("tq"):
            if not hasattr(models, "TurboQuantization"):
                raise RuntimeError(
                    "QDRANT_QUANTIZATION=tq* requires qdrant-client>=1.18. "
                    "Run: pip install -U 'qdrant-client>=1.18'"
                )

    def get_qdrant_client(self) -> AsyncQdrantClient:
        if self.qdrant_path is not None:
            return AsyncQdrantClient(path=self.qdrant_path, port=6333)
        elif self.url is not None:
            return AsyncQdrantClient(
                url=self.url,
                api_key=self.api_key,
                port=6333,
                timeout=self.timeout,
            )

        return AsyncQdrantClient(location=":memory:")

    async def embed_data(self, data: list[str]) -> list[float]:
        return await self.embedding_engine.embed_text(data)

    def _point_id(self, data_point_id: str | UUID) -> str:
        """Scope storage IDs to a dataset; payload IDs remain Cognee's IDs."""
        namespace = uuid5(NAMESPACE_URL, self.database_name)
        return str(uuid5(namespace, str(UUID(str(data_point_id)))))

    def _id_filter(self, point_ids: list[str]) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="database_name", match=models.MatchValue(value=self.database_name)
                ),
                models.HasIdCondition(has_id=point_ids),
            ]
        )

    async def has_collection(self, collection_name: str) -> bool:
        client = self.get_qdrant_client()
        result = await client.collection_exists(collection_name)
        await client.close()
        return result

    async def create_collection(
        self,
        collection_name: str,
        payload_schema=None,
    ):
        async with self.VECTOR_DB_LOCK:
            client = self.get_qdrant_client()

            if not await client.collection_exists(collection_name):
                await client.create_collection(
                    collection_name=collection_name,
                    vectors_config={
                        "text": models.VectorParams(
                            size=self.embedding_engine.get_vector_size(),
                            distance=models.Distance.COSINE,
                        )
                    },
                    # With this config definition, we avoid creating a global index
                    hnsw_config=models.HnswConfigDiff(
                        payload_m=16,
                        m=0,
                    ),
                    quantization_config=build_quantization_config(),
                )
                # This index co-locates vectors from the same dataset together,
                # which can improve performance
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name="database_name",
                    field_schema=models.KeywordIndexParams(
                        type=models.KeywordIndexType.KEYWORD,
                        is_tenant=True,
                    ),
                )

            await client.close()

    async def update_quantization(
        self,
        collection_name: str,
        quantization_config=None,
    ):
        """Apply or change the quantization config on an existing
        collection without recreating it. Qdrant re-indexes in the
        background; queries during rebuild may transparently fall back
        to full vectors."""
        config = (
            quantization_config if quantization_config is not None else build_quantization_config()
        )
        client = self.get_qdrant_client()
        try:
            await client.update_collection(
                collection_name=collection_name,
                quantization_config=config,
            )
        finally:
            await client.close()

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        from qdrant_client.http.exceptions import UnexpectedResponse

        client = self.get_qdrant_client()

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        def convert_to_qdrant_point(data_point: DataPoint):
            return models.PointStruct(
                id=self._point_id(data_point.id),
                payload={**data_point.model_dump(mode="json"), "database_name": self.database_name},
                vector={"text": data_vectors[data_points.index(data_point)]},
            )

        points = [convert_to_qdrant_point(point) for point in data_points]

        try:
            # Use upsert for AsyncQdrantClient (upload_points doesn't exist or is sync)
            await client.upsert(collection_name=collection_name, points=points, wait=True)
            # Migrate only this dataset's legacy, unscoped IDs after the new
            # points are durable. Another dataset may still own a legacy ID.
            if points:
                legacy_filter = self._id_filter([str(point.id) for point in data_points])
                legacy_filter.must_not = [models.HasIdCondition(has_id=[p.id for p in points])]
                await client.delete(
                    collection_name,
                    points_selector=models.FilterSelector(filter=legacy_filter),
                    wait=True,
                )
        except UnexpectedResponse as error:
            if "Collection not found" in str(error):
                raise CollectionNotFoundError(
                    message=f"Collection {collection_name} not found!"
                ) from error
            else:
                raise error
        except Exception as error:
            logger.error("Error uploading data points to Qdrant: %s", str(error))
            raise error
        finally:
            await client.close()

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

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        if not data_point_ids:
            return []
        original_ids = [str(UUID(str(point_id))) for point_id in data_point_ids]
        scoped_ids = [self._point_id(point_id) for point_id in original_ids]
        client = self.get_qdrant_client()
        try:
            results = await client.retrieve(
                collection_name, list(dict.fromkeys(original_ids + scoped_ids)), with_payload=True
            )
            # Retain legacy reads, but never return another dataset's points.
            by_id = {}
            for result in results:
                if not result.payload or result.payload.get("database_name") != self.database_name:
                    continue
                original_id = str(result.payload.get("id", result.id))
                if original_id not in original_ids:
                    continue
                if original_id not in by_id or str(result.id) == self._point_id(original_id):
                    by_id[original_id] = result.model_copy(update={"id": original_id})
            return list(by_id.values())
        finally:
            await client.close()

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = 15,
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

        client = None
        try:
            client = self.get_qdrant_client()
            if limit is None:
                collection_size = await client.count(collection_name=collection_name)
                limit = collection_size.count
            if limit == 0:
                await client.close()
                return []

            filters = [
                models.FieldCondition(
                    key="database_name",
                    match=models.MatchValue(
                        value=self.database_name,
                    ),
                )
            ]

            if node_name:
                if node_name_filter_operator == "AND":
                    must_conditions = [
                        models.FieldCondition(
                            key="belongs_to_set",
                            match=models.MatchAny(any=[name]),
                        )
                        for name in node_name
                    ]

                    filters.extend(must_conditions)
                else:
                    filters.append(
                        models.FieldCondition(
                            key="belongs_to_set", match=models.MatchAny(any=node_name)
                        )
                    )

            # Use query_points instead of search (API change in qdrant-client)
            # query_points is the correct method for AsyncQdrantClient
            query_result = await client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=models.Filter(must=filters),
                using="text",
                limit=limit,
                with_vectors=with_vector,
                # The payload ID maps the dataset-specific storage ID back to
                # the original graph ID, even when callers omit payloads.
                with_payload=True if include_payload else ["id"],
                search_params=build_search_params(),
            )

            await client.close()

            # Extract points from query_result
            results = query_result.points

            return [
                ScoredResult(
                    id=parse_id(str((result.payload or {}).get("id", result.id))),
                    payload=None
                    if not include_payload or not result.payload
                    else serialize_for_json(result.payload),
                    score=1 - result.score if hasattr(result, "score") else 1.0,
                )
                for result in results
            ]
        except Exception as e:
            logger.error(f"Error in Qdrant search: {e}", exc_info=True)
            if client:
                await client.close()
            return []

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list[list[ScoredResult]]:
        """Return one list of cosine-distance results per query, using Cognee embeddings."""
        if not query_texts:
            return []
        if limit == 0 or not await self.has_collection(collection_name):
            return [[] for _ in query_texts]

        query_vectors = await self.embed_data(query_texts)

        filters = [
            models.FieldCondition(
                key="database_name",
                match=models.MatchValue(
                    value=self.database_name,
                ),
            )
        ]

        if node_name:
            if node_name_filter_operator == "AND":
                must_conditions = [
                    models.FieldCondition(
                        key="belongs_to_set",
                        match=models.MatchAny(any=[name]),
                    )
                    for name in node_name
                ]

                filters.extend(must_conditions)
            else:
                filters.append(
                    models.FieldCondition(
                        key="belongs_to_set", match=models.MatchAny(any=node_name)
                    )
                )

        client = self.get_qdrant_client()
        try:
            query_filter = models.Filter(must=filters)
            if limit is None:
                collection_size = await client.count(
                    collection_name=collection_name, count_filter=query_filter, exact=True
                )
                limit = collection_size.count
            if limit == 0:
                return [[] for _ in query_texts]

            responses = await client.query_batch_points(
                collection_name=collection_name,
                requests=[
                    models.QueryRequest(
                        query=vector,
                        using="text",
                        filter=query_filter,
                        limit=limit,
                        with_vector=with_vectors,
                        with_payload=True if include_payload else ["id"],
                        params=build_search_params(),
                    )
                    for vector in query_vectors
                ],
            )
            return [
                [
                    ScoredResult(
                        id=parse_id(str((point.payload or {}).get("id", point.id))),
                        payload=serialize_for_json(point.payload)
                        if include_payload and point.payload
                        else None,
                        score=1 - point.score,
                    )
                    for point in response.points
                ]
                for response in responses
            ]
        finally:
            await client.close()

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        if not data_point_ids:
            return None
        original_ids = [str(UUID(str(point_id))) for point_id in data_point_ids]
        scoped_ids = [self._point_id(point_id) for point_id in original_ids]
        client = self.get_qdrant_client()
        try:
            return await client.delete(
                collection_name,
                points_selector=models.FilterSelector(
                    filter=self._id_filter(list(dict.fromkeys(original_ids + scoped_ids)))
                ),
                wait=True,
            )
        finally:
            await client.close()

    async def prune(self):
        client = self.get_qdrant_client()

        response = await client.get_collections()

        for collection in response.collections:
            await client.delete(
                collection.name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="database_name",
                                match=models.MatchValue(value=self.database_name),
                            )
                        ]
                    )
                ),
            )
            remaining_points = await client.count(collection_name=collection.name)
            if remaining_points.count == 0:
                await client.delete_collection(collection_name=collection.name)

        await client.close()

    async def get_collection_names(self) -> list[str]:
        """
        Get names of all collections in the database.

        Returns:
            list[str]: List of collection names.
        """

        client = self.get_qdrant_client()

        response = await client.get_collections()

        # We do this filtering because one user could see another user's collections otherwise
        result = []
        for collection in response.collections:
            relevant_count = await client.count(
                collection_name=collection.name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="database_name", match=models.MatchValue(value=self.database_name)
                        )
                    ]
                ),
                exact=True,
            )

            if relevant_count.count > 0:
                result.append(collection.name)

        await client.close()

        return result
