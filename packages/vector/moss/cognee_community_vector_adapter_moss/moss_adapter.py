import asyncio
import json
import time
from typing import List, Optional

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.shared.logging_utils import get_logger
from moss import (
    DocumentInfo,
    GetDocumentsOptions,
    MossClient,
    MutationOptions,
    QueryOptions,
)

logger = get_logger("MossAdapter")

JOB_POLL_INTERVAL = 2
JOB_POLL_TIMEOUT = 300
RETRY_COUNT = 3
RETRY_BACKOFF = 2


def _stringify_metadata(obj):
    """Flatten a dict so all values are strings (Moss requirement)."""
    result = {}
    for k, v in obj.items():
        if isinstance(v, str):
            result[str(k)] = v
        elif isinstance(v, (dict, list)):
            result[str(k)] = json.dumps(v)
        else:
            result[str(k)] = str(v)
    return result


class IndexSchema(DataPoint):
    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class MossAdapter(VectorDBInterface):
    name = "Moss"

    def __init__(
        self,
        url,
        api_key,
        embedding_engine: EmbeddingEngine,
        database_name: str = "cognee_db",
        **kwargs,
    ):
        # cognee >= 1.5.0 always passes vector_db_host / vector_db_port /
        # vector_db_username / vector_db_password to registered adapters. Moss is
        # a hosted service addressed by project id and project key, so they are
        # absorbed by **kwargs and ignored.
        self.embedding_engine = embedding_engine
        self.database_name = database_name
        self.api_key = api_key
        self.client = MossClient(project_id=database_name, project_key=api_key)
        self._index_name = f"cognee-index-{int(time.time())}"
        self._index_ready = False
        self._index_loaded = False
        self._collections: set[str] = set()
        self._create_lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()

    async def _find_existing_index(self):
        """Check if a cognee index already exists in the project."""
        if self._index_ready:
            return True
        try:
            indexes = await self.client.list_indexes()
            for idx in indexes:
                if idx.name.startswith("cognee-index-"):
                    self._index_name = idx.name
                    self._index_ready = True
                    return True
        except Exception:
            pass
        return False

    async def _create_index_with_docs(self, docs: list[DocumentInfo]):
        """Create the Moss index with the first batch of real documents."""
        async with self._create_lock:
            if self._index_ready:
                return
            try:
                logger.info("Creating Moss index '%s' with %d docs", self._index_name, len(docs))
                result = await self.client.create_index(self._index_name, docs)
                await self._wait_for_job(result.job_id)
                self._index_ready = True
                logger.info("Moss index '%s' created", self._index_name)
            except Exception as e:
                logger.error("Error creating Moss index: %s", str(e))
                raise

    async def _load_index(self):
        """Load the index locally for querying. Only needed before search/retrieve."""
        if self._index_loaded:
            return
        async with self._load_lock:
            if self._index_loaded:
                return
            logger.info("Loading Moss index '%s' locally", self._index_name)
            await self.client.load_index(self._index_name, auto_refresh=True)
            self._index_loaded = True
            logger.info("Moss index '%s' loaded", self._index_name)

    async def _wait_for_job(self, job_id: str):
        elapsed = 0
        while elapsed < JOB_POLL_TIMEOUT:
            resp = await self.client.get_job_status(job_id)
            status_val = resp.status.value
            if status_val == "completed":
                return resp
            if status_val == "failed":
                raise RuntimeError(f"Moss job {job_id} failed: {resp.error}")
            await asyncio.sleep(JOB_POLL_INTERVAL)
            elapsed += JOB_POLL_INTERVAL
        raise TimeoutError(f"Moss job {job_id} timed out after {JOB_POLL_TIMEOUT}s")

    async def embed_data(self, data: list[str]) -> list[float]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        if collection_name in self._collections:
            return True
        await self._find_existing_index()
        return self._index_ready

    async def create_collection(
        self,
        collection_name: str,
        payload_schema=None,
    ):
        self._collections.add(collection_name)

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        self._collections.add(collection_name)

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        docs = []
        for i, data_point in enumerate(data_points):
            raw = data_point.model_dump()
            payload = _stringify_metadata(
                {
                    **raw,
                    "_collection": collection_name,
                    "database_name": self.database_name,
                }
            )
            docs.append(
                DocumentInfo(
                    id=str(data_point.id),
                    text=DataPoint.get_embeddable_data(data_point),
                    metadata=payload,
                    embedding=data_vectors[i],
                )
            )

        try:
            if not self._index_ready:
                await self._find_existing_index()

            if not self._index_ready:
                await self._create_index_with_docs(docs)
            else:
                logger.info(
                    "Adding %d docs to Moss index '%s' [%s]",
                    len(docs),
                    self._index_name,
                    collection_name,
                )
                result = await self.client.add_docs(
                    self._index_name, docs, MutationOptions(upsert=True)
                )
                await self._wait_for_job(result.job_id)
                self._index_loaded = False
        except Exception as e:
            logger.error("Error adding data points to Moss: %s", str(e))
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
        if not self._index_ready:
            await self._find_existing_index()
        if not self._index_ready:
            return []
        await self._load_index()
        try:
            docs = await self.client.get_docs(
                self._index_name, GetDocumentsOptions(doc_ids=data_point_ids)
            )
            return [
                ScoredResult(
                    id=parse_id(doc.id),
                    payload=doc.metadata,
                    score=0.0,
                )
                for doc in docs
                if doc.metadata and doc.metadata.get("_collection") == collection_name
            ]
        except Exception as e:
            logger.error("Error retrieving from Moss: %s", str(e))
            return []

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

        await self._load_index()

        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]

        effective_limit = limit or 15
        if node_name:
            effective_limit = effective_limit * 5

        query_opts = QueryOptions(
            top_k=effective_limit,
            embedding=query_vector,
        )

        metadata_filter = self._build_filter(collection_name)
        if metadata_filter:
            query_opts.filter = metadata_filter

        last_err = None
        for attempt in range(RETRY_COUNT):
            try:
                result = await self.client.query(
                    self._index_name,
                    query_text or "",
                    query_opts,
                )

                scored = [
                    ScoredResult(
                        id=parse_id(doc.id),
                        payload=doc.metadata if include_payload else None,
                        score=1 - doc.score
                        if hasattr(doc, "score") and doc.score is not None
                        else 0.0,
                    )
                    for doc in result.docs
                ]

                if node_name:
                    pre_filter = len(scored)
                    scored = self._filter_by_node_name(
                        scored, result.docs, node_name, node_name_filter_operator
                    )
                    scored = scored[: limit or 15]
                    logger.info(
                        "Moss search '%s': %d results (%d before nodeset filter)",
                        collection_name,
                        len(scored),
                        pre_filter,
                    )
                else:
                    logger.info("Moss search '%s': %d results", collection_name, len(scored))

                return scored
            except Exception as e:
                last_err = e
                if "503" in str(e) or "502" in str(e) or "429" in str(e):
                    logger.warning(
                        "Moss search attempt %d/%d failed (retryable): %s",
                        attempt + 1,
                        RETRY_COUNT,
                        str(e),
                    )
                    await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
                logger.error("Error in Moss search: %s", str(e), exc_info=True)
                return []
        logger.error("Moss search failed after %d retries: %s", RETRY_COUNT, str(last_err))
        return []

    def _build_filter(self, collection_name: str) -> Optional[dict]:
        conditions = [
            {"field": "_collection", "condition": {"$eq": collection_name}},
            {"field": "database_name", "condition": {"$eq": self.database_name}},
        ]
        return {"$and": conditions}

    @staticmethod
    def _filter_by_node_name(
        scored: list[ScoredResult],
        raw_docs,
        node_name: List[str],
        operator: str,
    ) -> list[ScoredResult]:
        """Post-query filter on belongs_to_set (stored as JSON string in Moss metadata)."""
        filtered = []
        for sr, doc in zip(scored, raw_docs, strict=False):
            raw = doc.metadata.get("belongs_to_set", "[]") if doc.metadata else "[]"
            try:
                doc_sets = set(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                doc_sets = set()

            requested = set(node_name)
            if operator == "AND":
                if requested.issubset(doc_sets):
                    filtered.append(sr)
            else:
                if requested & doc_sets:
                    filtered.append(sr)
        return filtered

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ):
        results = await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_text=text,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                )
                for text in query_texts
            ]
        )
        return list(results)

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        if not self._index_ready:
            await self._find_existing_index()
        if not self._index_ready:
            return
        try:
            result = await self.client.delete_docs(self._index_name, data_point_ids)
            return result
        except Exception as e:
            logger.error("Error deleting from Moss: %s", str(e))
            raise

    async def prune(self):
        logger.info("Pruning Moss indexes")
        try:
            indexes = await self.client.list_indexes()
            for index in indexes:
                if index.name.startswith("cognee-index-"):
                    if self._index_loaded:
                        await self.client.unload_index(index.name)
                    await self.client.delete_index(index.name)
            self._collections.clear()
            self._index_ready = False
            self._index_loaded = False
        except RuntimeError as e:
            if "403" in str(e) or "Authentication" in str(e):
                logger.warning("Moss prune skipped (invalid credentials): %s", str(e))
            else:
                raise
        except Exception as e:
            logger.error("Error pruning Moss indexes: %s", str(e))
            raise
