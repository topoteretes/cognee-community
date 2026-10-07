"""Amazon S3 Vectors adapter for cognee.

Maps cognee's collection model onto S3 Vectors resources: one vector bucket
per adapter instance (named after ``database_name`` / ``vector_bucket_name``)
and one vector index per cognee collection. Search uses the native
``QueryVectors`` operation and pushes ``node_name`` filtering down to S3
Vectors' metadata filter DSL, so no client-side over-fetch is needed.
"""

import asyncio
import json
import os
import re

import boto3
from botocore.config import Config
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError,
)
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.modules.storage.utils import get_own_properties
from cognee.shared.logging_utils import get_logger

logger = get_logger("S3VectorsAdapter")

# S3 Vectors service limits (AWS docs: s3-vectors-limitations).
PUT_BATCH_SIZE = 500
DELETE_BATCH_SIZE = 500
GET_BATCH_SIZE = 100
MAX_TOP_K = 10_000

# The data-point payload is stored under this metadata key. It is the only
# non-filterable key configured on every index: the filterable metadata budget
# (2 KB per vector) is far smaller than the total budget (40 KB per vector) and
# full payloads regularly exceed it, while filters only ever need
# ``belongs_to_set``.
PAYLOAD_KEY = "payload"
BELONGS_TO_SET_KEY = "belongs_to_set"


class IndexSchema(DataPoint):
    id: str
    text: str
    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: list[str] = []


class S3VectorsAdapter(VectorDBInterface):
    name = "S3Vectors"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        embedding_engine: EmbeddingEngine = None,
        database_name: str | None = None,
        vector_bucket_name: str | None = None,
        region_name: str | None = None,
        **kwargs,
    ):
        """Create the adapter; credentials resolve lazily via boto3.

        ``url`` is an optional endpoint override (VPC/dual-stack endpoints).
        S3 Vectors authenticates with standard AWS IAM credentials, resolved
        through boto3's chain (environment, shared config, IAM roles); callers
        who keep credentials in cognee's config instead can pass the access key
        id as ``vector_db_username`` and the secret as ``vector_db_password``
        (or ``api_key``). ``vector_bucket_name`` defaults to ``database_name``.
        The region comes from ``region_name``, ``AWS_REGION`` /
        ``AWS_DEFAULT_REGION``, else ``us-east-1``.
        """
        if embedding_engine is None:
            raise ValueError("Missing required S3 Vectors embedding engine!")

        self.embedding_engine = embedding_engine
        self.endpoint_url = self._as_endpoint_url(url or kwargs.get("utl"))
        self.region_name = (
            region_name
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        self.vector_bucket_name = self._sanitize_name(
            vector_bucket_name or database_name or "cognee-vectors"
        )

        access_key_id = kwargs.get("vector_db_username") or None
        secret_access_key = kwargs.get("vector_db_password") or api_key or None

        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError(
                "Incomplete S3 Vectors credentials: set both VECTOR_DB_USERNAME "
                "(access key id) and VECTOR_DB_KEY / VECTOR_DB_PASSWORD (secret "
                "access key), or leave both unset to use the standard AWS "
                "credential chain."
            )

        session_kwargs = {"region_name": self.region_name}
        if access_key_id:
            session_kwargs["aws_access_key_id"] = access_key_id
            session_kwargs["aws_secret_access_key"] = secret_access_key
        if kwargs.get("aws_session_token"):
            session_kwargs["aws_session_token"] = kwargs["aws_session_token"]

        # boto3 resolves credentials and the endpoint lazily, so building the
        # client here needs neither network access nor valid credentials.
        client_kwargs = {"config": Config(retries={"max_attempts": 5, "mode": "standard"})}
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        self.client = boto3.Session(**session_kwargs).client("s3vectors", **client_kwargs)
        self.VECTOR_DB_LOCK = asyncio.Lock()

    @staticmethod
    def _as_endpoint_url(url: str | None) -> str | None:
        """Keep endpoint overrides only when they are actual URLs.

        The factory forwards ``VECTOR_DB_URL`` for every provider, and cognee
        fills that in with the default LanceDB path when it is unset, so a
        filesystem path must not be passed to boto3 as an endpoint.
        """
        if url and "://" in url:
            return url
        return None

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Fit a collection/database name into S3 Vectors' naming rules.

        Vector bucket and index names allow lowercase letters, digits and
        hyphens, must start and end with a letter or digit, and must be 3-63
        characters long.
        """
        sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())
        sanitized = re.sub(r"-+", "-", sanitized).strip("-")

        if len(sanitized) < 3:
            sanitized = f"idx-{sanitized}" if sanitized else "cognee-vectors"

        return sanitized[:63].rstrip("-") or "cognee-vectors"

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        """Check if a vector index exists (collection in S3 Vectors is an index)."""
        try:
            await asyncio.to_thread(
                self.client.get_index,
                vectorBucketName=self.vector_bucket_name,
                indexName=self._sanitize_name(collection_name),
            )
            return True
        except self.client.exceptions.NotFoundException:
            return False

    async def _ensure_vector_bucket(self):
        try:
            await asyncio.to_thread(
                self.client.get_vector_bucket, vectorBucketName=self.vector_bucket_name
            )
            return
        except self.client.exceptions.NotFoundException:
            pass

        try:
            await asyncio.to_thread(
                self.client.create_vector_bucket, vectorBucketName=self.vector_bucket_name
            )
            logger.info(f"Created S3 vector bucket: {self.vector_bucket_name}")
        except self.client.exceptions.ConflictException:
            # Another writer created the bucket between the two calls.
            pass

    async def create_collection(self, collection_name: str, payload_schema=None):
        """Create a vector index (with its bucket) with cosine/float32 config."""
        async with self.VECTOR_DB_LOCK:
            if await self.has_collection(collection_name):
                return

            await self._ensure_vector_bucket()

            try:
                await asyncio.to_thread(
                    self.client.create_index,
                    vectorBucketName=self.vector_bucket_name,
                    indexName=self._sanitize_name(collection_name),
                    dataType="float32",
                    dimension=self.embedding_engine.get_vector_size(),
                    distanceMetric="cosine",
                    metadataConfiguration={"nonFilterableMetadataKeys": [PAYLOAD_KEY]},
                )
            except self.client.exceptions.ConflictException:
                # Index already exists (created concurrently); create is idempotent.
                return

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        """Upsert data points as vectors, batching ``PutVectors`` calls."""
        if not data_points:
            return

        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name)

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        vectors = []
        for i, data_point in enumerate(data_points):
            properties = get_own_properties(data_point)

            # Normalize node-set membership to plain strings so the filterable
            # belongs_to_set key can back node_name filtering in search().
            raw_belongs_to_set = getattr(data_point, "belongs_to_set", None) or []
            belongs_to_set = [str(getattr(item, "name", item)) for item in raw_belongs_to_set]

            vectors.append(
                {
                    "key": str(data_point.id),
                    "data": {"float32": data_vectors[i]},
                    "metadata": {
                        BELONGS_TO_SET_KEY: belongs_to_set,
                        # Properties can hold UUIDs and other non-JSON types;
                        # S3 Vectors only accepts flat scalars/arrays, hence the
                        # JSON-string encoding (mirrors the azureaisearch adapter).
                        PAYLOAD_KEY: json.dumps(properties, default=str),
                    },
                }
            )

        for start in range(0, len(vectors), PUT_BATCH_SIZE):
            await asyncio.to_thread(
                self.client.put_vectors,
                vectorBucketName=self.vector_bucket_name,
                indexName=self._sanitize_name(collection_name),
                vectors=vectors[start : start + PUT_BATCH_SIZE],
            )

    async def retrieve(self, collection_name: str, data_point_ids: list[str]) -> list[ScoredResult]:
        """Retrieve documents by their IDs, batching ``GetVectors`` calls."""
        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        results = []
        for start in range(0, len(data_point_ids), GET_BATCH_SIZE):
            batch = [str(key) for key in data_point_ids[start : start + GET_BATCH_SIZE]]
            response = await asyncio.to_thread(
                self.client.get_vectors,
                vectorBucketName=self.vector_bucket_name,
                indexName=self._sanitize_name(collection_name),
                keys=batch,
                returnMetadata=True,
            )

            for vector in response.get("vectors", []):
                results.append(
                    ScoredResult(
                        id=parse_id(vector["key"]),
                        payload=self._parse_payload(
                            (vector.get("metadata") or {}).get(PAYLOAD_KEY)
                        ),
                        score=0,  # No score for direct retrieval
                    )
                )

        return results

    @staticmethod
    def _parse_payload(raw_payload) -> dict:
        if isinstance(raw_payload, str) and raw_payload:
            try:
                parsed = json.loads(raw_payload)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        return {}

    @staticmethod
    def _build_node_name_filter(node_name: list[str], node_name_filter_operator: str) -> dict:
        """Build an S3 Vectors metadata filter restricting results to data
        points whose ``belongs_to_set`` array contains ANY (operator ``OR``) or
        ALL (operator ``AND``) of ``node_name``.

        ``$in`` matches when any array element equals one of the given values.
        ``$eq`` on array metadata matches when any element equals the value, so
        chaining one ``$eq`` clause per name under ``$and`` requires every
        requested name to be present.
        """
        if node_name_filter_operator == "AND":
            return {"$and": [{BELONGS_TO_SET_KEY: {"$eq": name}} for name in node_name]}

        return {BELONGS_TO_SET_KEY: {"$in": list(node_name)}}

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
        normalized: bool = True,
    ) -> list[ScoredResult]:
        """Perform a vector search via S3 Vectors' ``QueryVectors``.

        Args:
            node_name: When provided, results are filtered server-side to data
                points whose ``belongs_to_set`` array contains ANY (operator
                ``OR``) or ALL (operator ``AND``) of these names.
            node_name_filter_operator: ``"OR"`` (default) or ``"AND"``,
                controlling how ``node_name`` entries are combined.
            normalized: When True (default), returns S3 Vectors' cosine
                distance (``1 - cosine_similarity``, lower is better), matching
                cognee's ``ScoredResult`` contract. When False, returns the
                cosine similarity (``1 - distance``, higher is better).
        """
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if limit is not None and limit <= 0:
            return []

        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        if query_vector is None and query_text:
            query_vector = (await self.embed_data([query_text]))[0]

        top_k = min(max(limit if limit is not None else MAX_TOP_K, 1), MAX_TOP_K)

        filter_expression = None
        if node_name:
            filter_expression = self._build_node_name_filter(node_name, node_name_filter_operator)

        results = []
        next_token = None
        while True:
            request = {
                "vectorBucketName": self.vector_bucket_name,
                "indexName": self._sanitize_name(collection_name),
                "queryVector": {"float32": query_vector},
                "topK": top_k,
                "returnDistance": True,
                # Metadata carries the payload; skipping it also drops the
                # GetVectors permission requirement for unfiltered queries.
                "returnMetadata": bool(include_payload),
            }
            if filter_expression is not None:
                request["filter"] = filter_expression
            if next_token is not None:
                request["nextToken"] = next_token

            response = await asyncio.to_thread(self.client.query_vectors, **request)

            for vector in response.get("vectors", []):
                results.append(self._to_scored_result(vector, normalized, include_payload))

            # QueryVectors pages at 100 results; keep following nextToken until
            # the requested limit is reached or the result set is exhausted.
            next_token = response.get("nextToken") or None
            if not next_token or len(results) >= top_k:
                break

        return results[:top_k]

    def _to_scored_result(
        self, vector: dict, normalized: bool, include_payload: bool
    ) -> ScoredResult:
        distance = float(vector.get("distance", 0.0))

        payload = None
        if include_payload:
            metadata = vector.get("metadata") or {}
            payload = self._parse_payload(metadata.get(PAYLOAD_KEY))
            payload["id"] = parse_id(vector["key"])

        return ScoredResult(
            id=parse_id(vector["key"]),
            payload=payload,
            score=distance if normalized else 1.0 - distance,
        )

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
    ) -> list[list[ScoredResult]]:
        """Perform a batch vector search."""
        query_vectors = await self.embed_data(query_texts)

        # S3 Vectors has no native batch query; parallelize individual searches.
        return await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                )
                for query_vector in query_vectors
            ]
        )

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        """Delete vectors by key, batching ``DeleteVectors`` calls.

        Idempotent for missing collections and empty id lists.
        """
        if not await self.has_collection(collection_name):
            return
        if not data_point_ids:
            return

        for start in range(0, len(data_point_ids), DELETE_BATCH_SIZE):
            batch = [str(key) for key in data_point_ids[start : start + DELETE_BATCH_SIZE]]
            await asyncio.to_thread(
                self.client.delete_vectors,
                vectorBucketName=self.vector_bucket_name,
                indexName=self._sanitize_name(collection_name),
                keys=batch,
            )

    async def create_vector_index(self, index_name: str, index_property_name: str):
        """Create a vector index for a specific property."""
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        """Index data points for a specific property."""
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=str(data_point.id),
                    text=getattr(data_point, data_point.metadata["index_fields"][0]),
                    belongs_to_set=(data_point.belongs_to_set or []),
                )
                for data_point in data_points
            ],
        )

    async def prune(self):
        """Delete every vector index in the adapter's bucket."""
        try:
            index_names = []
            next_token = None
            while True:
                request = {"vectorBucketName": self.vector_bucket_name}
                if next_token is not None:
                    request["nextToken"] = next_token

                response = await asyncio.to_thread(self.client.list_indexes, **request)
                index_names.extend(index["indexName"] for index in response.get("indexes", []))

                next_token = response.get("nextToken") or None
                if not next_token:
                    break
        except self.client.exceptions.NotFoundException:
            # The bucket doesn't exist yet: nothing to prune.
            return
        except Exception as error:
            logger.error(f"Error during prune operation: {str(error)}")
            raise

        for index_name in index_names:
            try:
                await asyncio.to_thread(
                    self.client.delete_index,
                    vectorBucketName=self.vector_bucket_name,
                    indexName=index_name,
                )
                logger.info(f"Deleted index: {index_name}")
            except Exception as error:
                logger.error(f"Error deleting index {index_name}: {str(error)}")
