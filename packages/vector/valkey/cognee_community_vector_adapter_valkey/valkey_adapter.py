from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.shared.logging_utils import get_logger
from glide import (
    BackoffStrategy,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    ft,
    glide_json,
)
from glide_shared.commands.server_modules.ft_options.ft_create_options import (
    DataType,
    DistanceMetricType,
    FtCreateOptions,
    TagField,
    VectorAlgorithm,
    VectorField,
    VectorFieldAttributesHnsw,
    VectorType,
)
from glide_shared.commands.server_modules.ft_options.ft_search_options import (
    FtSearchOptions,
    ReturnField,
)
from glide_shared.exceptions import RequestError
from glide_shared.exceptions import TimeoutError as GlideTimeoutError

from .exceptions import CollectionNotFoundError, ValkeyVectorEngineInitializationError
from .utils import (
    _build_scored_results_from_ft,
    _parse_host_port,
    _serialize_for_json,
    _to_float32_bytes,
)

logger = get_logger("ValkeyAdapter")


class IndexSchema(DataPoint):
    """Minimal data point written by index_data_points: one indexed text field."""

    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class ValkeyAdapter(VectorDBInterface):
    """Valkey vector database adapter using ValkeyGlide for vector similarity search.

    This adapter provides an implementation of the `VectorDBInterface` for Valkey,
    enabling vector storage, retrieval, and similarity search using Valkey's
    full-text and vector indexing capabilities.
    """

    name = "Valkey"
    url: str | None
    api_key: str | None = None
    embedding_engine: EmbeddingEngine | None = None

    def __init__(
        self,
        url: str | None,
        api_key: str | None = None,
        database_name: str = "cognee",
        embedding_engine: EmbeddingEngine | None = None,
    ) -> None:
        """Initialize the Valkey adapter.

        Args:
            url (str): Connection string for your Valkey instance like valkey://localhost:6379.
            embedding_engine: Engine for generating embeddings.
            api_key: Optional API key. Ignored for Valkey.

        Raises:
            ValkeyVectorEngineInitializationError: If required parameters are missing.
        """

        if not embedding_engine:
            raise ValkeyVectorEngineInitializationError(
                "Embedding engine is required. Provide 'embedding_engine' to the Valkey adapter."
            )

        self.url = url
        self._host, self._port = _parse_host_port(url)
        self.database_name = database_name
        self.embedding_engine = embedding_engine
        self._client: GlideClient | None = None
        self._connected = False
        self.VECTOR_DB_LOCK = asyncio.Lock()

    # -------------------- lifecycle --------------------

    async def get_connection(self) -> GlideClient:
        """Establish and return an asynchronous Glide client connection to the Valkey server.

        If a connection already exists and is marked as active, it will be reused.
        Otherwise, a new connection is created using the configured host and port.

        Returns:
            GlideClient: An active Glide client instance for executing Valkey commands.

        Behavior:
            - Uses a backoff reconnect strategy with 3 retries and exponential delay.
            - Disables TLS by default (set `use_tls=True` in configuration if needed).
            - Sets a request timeout of 5000 ms.
        """

        if self._connected and self._client is not None:
            return self._client

        cfg = GlideClientConfiguration(
            [NodeAddress(self._host, self._port)],
            client_name="cognee_vector_store_client",
            use_tls=False,
            # Documents carry full embedding vectors (tens of KB each); a slow
            # CI runner needs headroom or bulk writes hit "TimeoutError: timed
            # out" mid-pipeline.
            request_timeout=30000,
            reconnect_strategy=BackoffStrategy(num_of_retries=3, factor=1000, exponent_base=2),
        )
        self._client = await GlideClient.create(cfg)
        self._connected = True

        return self._client

    async def close(self) -> None:
        """Close the active Glide client connection to the Valkey server.

        If a client connection exists, attempts to close it gracefully.
        Any exceptions during closure are suppressed to avoid breaking cleanup logic.

        After closing:
            - The internal client reference is set to None.
            - The connection state flag (`_connected`) is reset to False.

        Returns:
            None

        """

        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                logger.error("Failed to close Valkey client: %e", e)
                pass
        self._client = None
        self._connected = False

    # -------------------- helpers --------------------

    def _index_name(self, collection: str) -> str:
        return f"index:{collection}"

    def _key_prefix(self, collection: str) -> str:
        return f"vdb:{collection}:"

    def _key(self, collection: str, pid: str) -> str:
        return f"{self._key_prefix(collection)}{pid}"

    def _ensure_dims(self) -> int:
        dims = self.embedding_engine.get_dimensions()
        return int(dims)

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        """Embed text data using the embedding engine.

        Args:
            data: List of text strings to embed.

        Returns:
            List of embedding vectors as lists of floats.

        Raises:
            Exception: If embedding generation fails.
        """
        return await self.embedding_engine.embed_text(data)

    # -------------------- VectorDBInterface methods --------------------

    async def has_collection(self, collection_name: str) -> bool:
        """Check if a collection (index) exists.

        Args:
            collection_name: Name of the collection to check.

        Returns:
            True if collection exists, False otherwise.
        """
        client = await self.get_connection()
        try:
            await ft.info(client, self._index_name(collection_name))
            return True
        except Exception as e:
            logger.warning("Valkey index check failed for '%s': %s", collection_name, e)
            return False

    async def create_collection(
        self,
        collection_name: str,
        payload_schema: Any | None = None,
    ) -> None:
        """Create a new collection (Valkey index) with vector search capabilities.

        Args:
            collection_name: Name of the collection to create.
            payload_schema: Schema for payload data (not used).

        Raises:
            Exception: If collection creation fails.
        """
        async with self.VECTOR_DB_LOCK:
            try:
                if await self.has_collection(collection_name):
                    logger.info(f"Collection {collection_name} already exists")
                    return

                fields = [
                    TagField("id"),
                    # Index belongs_to_set as a comma-separated tag scalar. valkey-search does
                    # not reliably index multi-element JSON arrays via "$.belongs_to_set[*]"
                    # (single-element works, multi-element does not), so create_data_points
                    # stores the set names as a single comma-joined string and we declare the
                    # separator here so each name becomes its own tag.
                    TagField("$.belongs_to_set", alias="belongs_to_set", separator=","),
                    VectorField(
                        name="vector",
                        algorithm=VectorAlgorithm.HNSW,
                        attributes=VectorFieldAttributesHnsw(
                            dimensions=self.embedding_engine.get_vector_size(),
                            distance_metric=DistanceMetricType.COSINE,
                            type=VectorType.FLOAT32,
                        ),
                    ),
                ]
                prefixes = [self._key_prefix(collection_name)]
                options = FtCreateOptions(DataType.JSON, prefixes)
                index = self._index_name(collection_name)

                ok = await ft.create(self._client, index, fields, options)
                if ok not in (b"OK", "OK"):
                    raise Exception(f"FT.CREATE failed for index '{index}': {ok!r}")

            except Exception as e:
                logger.error(f"Error creating collection {collection_name}: {str(e)}")
                raise e

    async def create_data_points(
        self,
        collection_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Create data points in the collection.

        Args:
            collection_name: Name of the target collection.
            data_points: List of DataPoint objects to insert.

        Raises:
            CollectionNotFoundError: If the collection doesn't exist.
            Exception: If data point creation fails.
        """
        client = await self.get_connection()
        assert self._client is not None

        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(f"Collection {collection_name} not found!")

            # Embed the data points
            data_to_embed = [
                DataPoint.get_embeddable_data(data_point) for data_point in data_points
            ]
            data_vectors = await self.embed_data(data_to_embed)

            # Bound write concurrency: firing every JSON.SET at once over a
            # single connection makes bulk indexing (e.g. cognee's
            # index_graph_edges) exceed the request timeout on slow runners.
            write_semaphore = asyncio.Semaphore(32)

            async def _write_document(data_point, embedding):
                payload = _serialize_for_json(data_point.model_dump())

                doc_data = {
                    "id": str(data_point.id),
                    "vector": embedding,
                    # Store belongs_to_set as a comma-separated string so the TAG field
                    # (declared with separator=",") indexes each set name individually.
                    # Commas inside a name would corrupt the split, but node-set names do
                    # not contain commas in practice.
                    "belongs_to_set": ",".join(
                        str(x) for x in (getattr(data_point, "belongs_to_set", None) or [])
                    ),
                    "payload_data": json.dumps(payload),  # Store as JSON string
                }

                # Retry on glide timeouts: when another pipeline task blocks
                # the event loop with long synchronous work (e.g. an embedded
                # graph engine), in-flight glide requests can hit their
                # client-side timer even though the server is healthy. The
                # write is an idempotent JSON.SET, so retrying is safe.
                for attempt in range(4):
                    try:
                        async with write_semaphore:
                            await glide_json.set(
                                client,
                                self._key(collection_name, str(data_point.id)),
                                "$",
                                json.dumps(doc_data),
                            )
                        break
                    except GlideTimeoutError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.5 * (attempt + 1))

            await asyncio.gather(
                *[
                    _write_document(data_point, embedding)
                    for data_point, embedding in zip(data_points, data_vectors, strict=False)
                ]
            )

        except RequestError as e:
            # Helpful guidance if JSON vector arrays aren't supported by the deployed module
            logger.error(f"JSON.SET failed: {e}")
            raise e

        except Exception as e:
            logger.error(f"Error creating data points: {str(e)}")
            raise e

    async def create_vector_index(self, index_name: str, index_property_name: str):
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: List[DataPoint]
    ):
        """Index data points into the per-field collection created by create_vector_index.

        Without this override the interface default is a no-op and cognify
        would silently store nothing searchable.
        """
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

    async def retrieve(
        self,
        collection_name: str,
        data_point_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Retrieve data points by their IDs.

        Args:
            collection_name: Name of the collection to retrieve from.
            data_point_ids: List of data point IDs to retrieve.

        Returns:
            List of retrieved data point payloads.
        """
        client = await self.get_connection()
        assert self._client is not None

        try:
            results = []
            for data_id in data_point_ids:
                key = self._key(collection_name, data_id)
                raw_doc = await glide_json.get(client, key, "$")
                if raw_doc:
                    doc = json.loads(raw_doc)
                    payload_str = doc[0]["payload_data"]
                    try:
                        payload = json.loads(payload_str)
                        results.append(payload)
                    except json.JSONDecodeError:
                        # Fallback to the document itself if payload parsing fails
                        results.append(raw_doc)

            return results

        except Exception as e:
            logger.error(f"Error retrieving data points: {str(e)}")
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
        """Search for similar vectors in the collection.

        Args:
            collection_name: Name of the collection to search.
            query_text: Text query to search for (will be embedded).
            query_vector: Pre-computed query vector.
            limit: Maximum number of results to return.
            with_vector: Whether to include vectors in results.
            include_payload: Whether to include payloads in results.

        Returns:
            List of ScoredResult objects sorted by similarity.

        Raises:
            MissingQueryParameterError: If neither query_text nor query_vector is provided.
            Exception: If search execution fails.
        """
        client = await self.get_connection()
        assert self._client is not None

        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            logger.warning(
                f"Collection '{collection_name}' not found in ValkeyAdapter.search; returning []."
            )
            return []

        if limit is None:
            info = await ft.info(client, self._index_name(collection_name))
            limit = info["num_docs"]

        if limit <= 0:
            return []

        try:
            # Get the query vector
            if query_vector is None:
                [vec] = await self.embed_data([query_text])
            else:
                vec = query_vector
            vec_bytes = _to_float32_bytes(vec)

            # Set return fields
            return_fields = [
                ReturnField("$.id", alias="id"),
                ReturnField("__vector_score", alias="score"),
            ]
            if include_payload:
                return_fields.append(ReturnField("$.payload_data", alias="payload_data"))
            if with_vector:
                return_fields.append(ReturnField("$.vector", alias="vector"))

            # Build an optional pre-filter on the belongs_to_set tag field so that only
            # data points whose belongs_to_set contains ANY ("OR") or ALL ("AND") of the
            # provided node_name values are considered. When node_name is falsy, match all ("*").
            prefilter = "*"
            if node_name:

                def _escape_tag(value: str) -> str:
                    # Escape RediSearch/Valkey tag query special characters.
                    escaped = []
                    for ch in str(value):
                        if ch in {"|", "{", "}", "(", ")", " ", "\\", "-", "@", ":"}:
                            escaped.append("\\" + ch)
                        else:
                            escaped.append(ch)
                    return "".join(escaped)

                names = [_escape_tag(name) for name in node_name]
                if node_name_filter_operator == "AND":
                    # AND: every name must be present (space-separated clauses are intersected).
                    prefilter = " ".join(f"@belongs_to_set:{{{name}}}" for name in names)
                else:
                    # OR: any name may match. valkey-search does not honor an in-brace
                    # "{a|b}" union, so use query-level union of separate tag clauses.
                    prefilter = " | ".join(f"@belongs_to_set:{{{name}}}" for name in names)
                prefilter = f"({prefilter})"

            vector_param_name = "query_vector"
            query = f"{prefilter}=>[KNN {limit} @vector ${vector_param_name}]"
            query_options = FtSearchOptions(
                params={vector_param_name: vec_bytes}, return_fields=return_fields
            )

            # Execute the search
            raw_results = await ft.search(
                client=client,
                index_name=self._index_name(collection_name),
                query=query,
                options=query_options,
            )

            scored_results = _build_scored_results_from_ft(raw_results)
            return scored_results

        except Exception as e:
            logger.error(f"Error during search: {str(e)}")
            raise e

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None,
        with_vectors: bool = False,
        score_threshold: float | None = 0.1,
        max_concurrency: int = 10,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
    ) -> list[list[ScoredResult]]:
        """Perform batch search for multiple queries.

        Args:
            collection_name: Name of the collection to search.
            query_texts: List of text queries to search for.
            limit: Maximum number of results per query.
            with_vectors: Whether to include vectors in results.
            score_threshold: threshold for filtering scores.
            max_concurrency: maximum number of concurrent searches.
            include_payload: Whether to include payloads in results.

        Returns:
            List of search results for each query, filtered by score threshold.
        """
        if not await self.has_collection(collection_name):
            logger.warning(
                f"Collection '{collection_name}' not found in ValkeyAdapter.search; returning []."
            )
            return []

        # Embed all queries at once
        vectors = await self.embed_data(query_texts)

        # Execute searches in parallel
        semaphore = asyncio.Semaphore(max_concurrency)

        async def limited_search(vector):
            async with semaphore:
                return await self.search(
                    collection_name=collection_name,
                    query_vector=vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                )

        tasks = [limited_search(vector) for vector in vectors]
        results = await asyncio.gather(*tasks)

        # Filter results by a score threshold
        return [
            [result for result in result_group if result.score < score_threshold]
            for result_group in results
        ]

    async def delete_data_points(
        self,
        collection_name: str,
        data_point_ids: list[str],
    ) -> dict[str, int]:
        """Delete data points by their IDs.

        Args:
            collection_name: Name of the collection to delete from.
            data_point_ids: List of data point IDs to delete.

        Returns:
            Dictionary containing the number of deleted documents.

        Raises:
            Exception: If deletion fails.
        """
        client = await self.get_connection()
        assert self._client is not None

        ids = [self._key(collection_name, id) for id in data_point_ids]

        try:
            deleted_count = await client.delete(ids)
            logger.info(f"Deleted {deleted_count} data points from collection {collection_name}")
            return {"deleted": deleted_count}
        except Exception as e:
            logger.error(f"Error deleting data points: {str(e)}")
            raise e

    async def prune(self):
        """Remove all collections and data from Valkey.

        This method drops all existing indices and clears the internal cache.

        Raises:
            Exception: If pruning fails.
        """
        client = await self.get_connection()
        assert self._client is not None
        try:
            all_indexes = await ft.list(client)
            for index in all_indexes:
                await ft.dropindex(client, index)
                logger.info(f"Dropped index {index}")

        except Exception as e:
            logger.error(f"Error during prune: {str(e)}")
            raise e
