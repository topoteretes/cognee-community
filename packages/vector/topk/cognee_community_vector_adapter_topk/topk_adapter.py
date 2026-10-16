import asyncio
import functools
import json
import operator
import os
from typing import List, Optional

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger
from topk_sdk import AsyncClient
from topk_sdk.error import CollectionAlreadyExistsError
from topk_sdk.error import CollectionNotFoundError as TopKCollectionNotFoundError
from topk_sdk.query import field, fn, select
from topk_sdk.schema import f32_vector, vector_index

logger = get_logger("TopKAdapter")

# 8MB per upsert request, 200KB per document.
_UPSERT_BATCH_BYTES = 6 * 1024 * 1024
_UPSERT_BATCH_DOCS = 100


class IndexSchema(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class TopKAdapter(VectorDBInterface):
    name = "TopK"

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        embedding_engine: EmbeddingEngine = None,
        database_name: str = "cognee_db",
    ):
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self.api_key = api_key
        self._prefix = f"{database_name}__"
        self._client = None
        self._lsns: dict[str, str] = {}

    def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(
                host=os.getenv("TOPK_HOST", "topk.io"),
                region=os.getenv("TOPK_REGION", "aws-us-east-1-elastica"),
                https=os.getenv("TOPK_HTTPS") != "false",
                api_key=self.api_key or os.getenv("TOPK_API_KEY", ""),
            )
        return self._client

    def _collection_name(self, collection_name: str) -> str:
        return self._prefix + collection_name

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        try:
            await self._get_client().collections().get(self._collection_name(collection_name))
            return True
        except TopKCollectionNotFoundError:
            return False

    async def create_collection(self, collection_name: str, payload_schema=None):
        try:
            await (
                self._get_client()
                .collections()
                .create(
                    self._collection_name(collection_name),
                    schema={
                        "vector": f32_vector(
                            dimension=self.embedding_engine.get_vector_size()
                        ).index(vector_index(metric="cosine")),
                    },
                )
            )
        except CollectionAlreadyExistsError:
            pass

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        if not data_points:
            return

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        documents = []
        for data_point, vector in zip(data_points, data_vectors, strict=True):
            payload = data_point.model_dump(mode="json")
            documents.append(
                {
                    "_id": str(data_point.id),
                    "vector": vector,
                    "payload": json.dumps(payload),
                    "belongs_to_set": payload.get("belongs_to_set") or [],
                }
            )

        prefixed_name = self._collection_name(collection_name)
        collection = self._get_client().collection(prefixed_name)

        for batch in _batch_documents(documents):
            try:
                self._lsns[prefixed_name] = await collection.upsert(batch)
            except TopKCollectionNotFoundError:
                await self.create_collection(collection_name)
                self._lsns[prefixed_name] = await collection.upsert(batch)
            except Exception as error:
                logger.error("Error uploading data points to TopK: %s", str(error))
                raise

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
        prefixed_name = self._collection_name(collection_name)
        try:
            documents = (
                await self._get_client()
                .collection(prefixed_name)
                .get(
                    [str(data_point_id) for data_point_id in data_point_ids],
                    lsn=self._lsns.get(prefixed_name),
                )
            )
        except TopKCollectionNotFoundError:
            return []

        results = []
        for document_id, document in documents.items():
            payload = json.loads(document.get("payload", "{}"))
            payload["id"] = document_id
            results.append(ScoredResult(id=parse_id(document_id), payload=payload, score=0))
        return results

    def _to_scored_results(self, documents, include_payload: bool) -> List[ScoredResult]:
        results = []
        for document in documents:
            payload = None
            if include_payload:
                payload = json.loads(document.get("payload", "{}"))
                payload["id"] = document["_id"]
            results.append(
                ScoredResult(
                    id=parse_id(document["_id"]),
                    payload=payload,
                    # TopK cosine returns similarity (higher = closer);
                    # ScoredResult.score is lower-is-better.
                    score=1 - document["similarity"],
                )
            )
        return results

    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        limit: Optional[int] = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> List[ScoredResult]:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if limit == 0:
            return []

        prefixed_name = self._collection_name(collection_name)
        collection = self._get_client().collection(prefixed_name)

        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]

        try:
            lsn = self._lsns.get(prefixed_name)

            if limit is None:
                limit = await collection.count(lsn=lsn)
                if limit == 0:
                    return []

            fields = ["payload"] if include_payload else []
            query = select(*fields, similarity=fn.vector_distance("vector", query_vector))

            if node_name:
                exprs = [field("belongs_to_set").contains(name) for name in node_name]
                operator = operator.and_ if node_name_filter_operator == "AND" else operator.or_
                query = query.filter(functools.reduce(operator, exprs))

            documents = await collection.query(
                query.sort(field("similarity"), asc=False).limit(limit),
                lsn=lsn,
            )
        except TopKCollectionNotFoundError as error:
            raise CollectionNotFoundError(f"Collection '{collection_name}' not found!") from error

        return self._to_scored_results(documents, include_payload)

    async def batch_search(
        self,
        collection_name: str,
        query_texts: List[str],
        limit: Optional[int] = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ):
        query_vectors = await self.embed_data(query_texts)
        return await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                )
                for query_vector in query_vectors
            ]
        )

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        prefixed_name = self._collection_name(collection_name)
        try:
            self._lsns[prefixed_name] = (
                await self._get_client()
                .collection(prefixed_name)
                .delete([str(data_point_id) for data_point_id in data_point_ids])
            )
        except TopKCollectionNotFoundError:
            pass
        except Exception as error:
            logger.error("Error deleting data points from TopK: %s", str(error))
            raise

    async def prune(self):
        client = self._get_client()
        try:
            for collection in await client.collections().list():
                if collection.name.startswith(self._prefix):
                    await client.collections().delete(collection.name)
        except Exception as error:
            logger.error("Error pruning TopK collections: %s", str(error))
            raise

    async def get_collection_names(self) -> list[str]:
        try:
            return [
                collection.name[len(self._prefix) :]
                for collection in await self._get_client().collections().list()
                if collection.name.startswith(self._prefix)
            ]
        except Exception as error:
            logger.error("Error listing TopK collections: %s", str(error))
            return []


def _batch_documents(documents: list[dict]) -> list[list[dict]]:
    batches = []
    batch, batch_bytes = [], 0
    for document in documents:
        document_bytes = len(document["payload"]) + 4 * len(document["vector"]) + 256
        if batch and (
            batch_bytes + document_bytes > _UPSERT_BATCH_BYTES or len(batch) >= _UPSERT_BATCH_DOCS
        ):
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(document)
        batch_bytes += document_bytes
    if batch:
        batches.append(batch)
    return batches
