import asyncio
import json
import re

import boto3
from botocore.exceptions import ClientError
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

# S3 Vectors' "index" is scoped to a single vector bucket (cognee's "collection"
# in the s3_vectors adapter shipped in mem0/mastra terms maps to an index, not
# a bucket -- see README). Each cognee collection therefore becomes one index
# inside ONE shared vector bucket, following the s3_vectors pattern already
# used by other AWS-native adapters (mem0's s3_vectors, one bucket per adapter
# instance, one index per collection) rather than one bucket per collection --
# S3 Vectors buckets are a heavier, account-scoped resource than indexes.


class S3VectorsAdapter(VectorDBInterface):
    """Cognee VectorDBInterface backed by Amazon S3 Vectors native vector
    search (`s3vectors` boto3 client, GA 2026). One vector bucket per adapter
    instance; one vector index per cognee collection.
    """

    name = "S3Vectors"

    def __init__(
        self,
        embedding_engine: EmbeddingEngine = None,
        database_name: str | None = None,
        vector_bucket_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        distance_metric: str = "cosine",
        **kwargs,
    ):
        """Cognee's factory (`create_vector_engine`) always constructs
        community adapters as
        `adapter_class(url=..., api_key=..., embedding_engine=..., database_name=...)`.
        S3 Vectors has no notion of a connection URL or API key (auth is via
        the standard boto3 credential chain), so `url`/`api_key` are accepted
        for contract compatibility but unused; `database_name` (cognee's
        generic "which database" config value) is the natural mapping onto
        S3 Vectors' vector bucket name, with an explicit `vector_bucket_name`
        kwarg as an override for direct (non-factory) construction.
        """
        final_bucket_name = vector_bucket_name or database_name or kwargs.get("url")
        if not (final_bucket_name and embedding_engine):
            raise ValueError("Missing required S3 Vectors adapter arguments!")

        self.vector_bucket_name = self._sanitize_bucket_name(final_bucket_name)
        self.embedding_engine = embedding_engine
        self.distance_metric = distance_metric.lower()
        if self.distance_metric not in ("cosine", "euclidean"):
            raise ValueError("distance_metric must be 'cosine' or 'euclidean'")

        self.client = boto3.client(
            "s3vectors",
            region_name=region_name,
            endpoint_url=endpoint_url,
        )
        self.VECTOR_DB_LOCK = asyncio.Lock()
        self._bucket_ready = False

    @staticmethod
    def _sanitize_bucket_name(name: str) -> str:
        """S3 vector bucket names follow S3 bucket naming rules: lowercase
        letters, digits, dots, and hyphens; 3-63 characters; must start and
        end with a letter or digit."""
        sanitized = re.sub(r"[^a-z0-9.-]", "-", name.lower())
        sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
        if len(sanitized) < 3:
            sanitized = f"{sanitized}-idx".ljust(3, "0")
        return sanitized[:63].rstrip("-.")

    @staticmethod
    def _sanitize_index_name(name: str) -> str:
        """Vector index names follow the same character rules as bucket names."""
        sanitized = re.sub(r"[^a-z0-9.-]", "-", name.lower())
        sanitized = re.sub(r"-+", "-", sanitized).strip("-.")
        if len(sanitized) < 3:
            sanitized = f"{sanitized}-idx".ljust(3, "0")
        return sanitized[:63].rstrip("-.")

    async def _ensure_bucket(self):
        if self._bucket_ready:
            return
        async with self.VECTOR_DB_LOCK:
            if self._bucket_ready:
                return
            try:
                await asyncio.to_thread(
                    self.client.create_vector_bucket,
                    vectorBucketName=self.vector_bucket_name,
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConflictException":
                    raise
            self._bucket_ready = True

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        """Check whether a vector index exists for this collection."""
        sanitized_name = self._sanitize_index_name(collection_name)
        try:
            await asyncio.to_thread(
                self.client.get_index,
                vectorBucketName=self.vector_bucket_name,
                indexName=sanitized_name,
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] == "NotFoundException":
                return False
            raise

    async def create_collection(self, collection_name: str, payload_schema=None):
        """Create a new vector index for this collection."""
        await self._ensure_bucket()

        async with self.VECTOR_DB_LOCK:
            sanitized_name = self._sanitize_index_name(collection_name)

            if await self.has_collection(collection_name):
                return

            vector_size = self.embedding_engine.get_vector_size()

            try:
                await asyncio.to_thread(
                    self.client.create_index,
                    vectorBucketName=self.vector_bucket_name,
                    indexName=sanitized_name,
                    dataType="float32",
                    dimension=vector_size,
                    distanceMetric=self.distance_metric,
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConflictException":
                    raise

    @staticmethod
    def _sanitize_metadata_value(value):
        """S3 Vectors' metadata document only accepts str/int/bool/float/list/
        dict -- coerce anything else (UUID, datetime, etc., which cognee's
        DataPoint fields commonly hold) to a JSON-safe representation. Caught
        via real-AWS testing: boto3's parameter validator rejects a raw UUID
        with a ParamValidationError; mocked tests never exercise real
        serialization and cannot catch this class of bug."""
        if isinstance(value, (str, int, bool, float)) or value is None:
            return value
        if isinstance(value, dict):
            return {k: S3VectorsAdapter._sanitize_metadata_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [S3VectorsAdapter._sanitize_metadata_value(v) for v in value]
        return str(value)

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        """Write data points into the vector index."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name)

        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        vectors = []
        for i, data_point in enumerate(data_points):
            properties = self._sanitize_metadata_value(get_own_properties(data_point))

            raw_belongs_to_set = getattr(data_point, "belongs_to_set", None) or []
            belongs_to_set = [str(getattr(item, "name", item)) for item in raw_belongs_to_set]

            # S3 Vectors metadata values must be flat: strings, numbers,
            # booleans, or arrays of those -- a nested object/dict is
            # rejected with ValidationException at PutVectors time (confirmed
            # via real-AWS testing, not documented explicitly by AWS). The
            # arbitrary-shaped `payload` dict is therefore JSON-serialized
            # into a string, mirroring the same pattern already used by the
            # azureaisearch adapter for the identical constraint.
            metadata = {
                "text": DataPoint.get_embeddable_data(data_point),
                "payload": json.dumps(properties),
                "belongs_to_set": belongs_to_set,
            }

            vectors.append(
                {
                    "key": str(data_point.id),
                    "data": {"float32": [float(v) for v in data_vectors[i]]},
                    "metadata": metadata,
                }
            )

        # put_vectors has a per-request item cap; batch defensively rather
        # than assuming an unbounded single call always succeeds.
        batch_size = 500
        for start in range(0, len(vectors), batch_size):
            batch = vectors[start : start + batch_size]
            await asyncio.to_thread(
                self.client.put_vectors,
                vectorBucketName=self.vector_bucket_name,
                indexName=sanitized_name,
                vectors=batch,
            )

    @staticmethod
    def _parse_payload(metadata: dict) -> dict:
        """Parse the JSON-serialized `payload` field back into a dict."""
        payload_raw = (metadata or {}).get("payload", "{}")
        if isinstance(payload_raw, dict):
            return dict(payload_raw)
        try:
            return json.loads(payload_raw)
        except (TypeError, json.JSONDecodeError):
            return {}

    async def retrieve(self, collection_name: str, data_point_ids: list[str]) -> list[ScoredResult]:
        """Retrieve vectors by key."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        try:
            response = await asyncio.to_thread(
                self.client.get_vectors,
                vectorBucketName=self.vector_bucket_name,
                indexName=sanitized_name,
                keys=[str(i) for i in data_point_ids],
                returnMetadata=True,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "NotFoundException":
                return []
            raise

        results = []
        for vector in response.get("vectors", []):
            payload = self._parse_payload(vector.get("metadata"))
            payload["id"] = parse_id(vector["key"])
            results.append(ScoredResult(id=parse_id(vector["key"]), payload=payload, score=0))

        return results

    @staticmethod
    def _build_node_name_filter(node_name: list[str], node_name_filter_operator: str) -> dict:
        """Build an S3 Vectors metadata filter restricting results to data
        points whose `belongs_to_set` metadata list contains ANY (`OR`, via
        `$in`) or ALL (`AND`, via `$and` of per-name `$eq`... actually `$in`
        against a list-valued field matches on element membership, so ANY of
        `node_name` is a single `$in`; ALL requires one `$eq`-style membership
        check per name, ANDed together).
        """
        if node_name_filter_operator == "AND":
            return {"$and": [{"belongs_to_set": {"$eq": name}} for name in node_name]}

        return {"belongs_to_set": {"$in": node_name}}

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
    ) -> list[ScoredResult]:
        """Perform an approximate nearest neighbor search.

        Args:
            node_name: when provided, results are filtered server-side to
                data points whose `belongs_to_set` metadata list contains ANY
                (`"OR"`, default) or ALL (`"AND"`) of these names, via S3
                Vectors' native metadata filter -- no client-side filtering
                is needed, unlike stores without a native filter DSL.

        Score semantics (NOTE: AWS does not currently document this explicitly
        in the S3Vectors API reference -- verified via the standard cosine-
        distance identity and cross-checked against community migration
        reports, but confirmed empirically against a live index in this PR's
        real-AWS test run before relying on it): S3 Vectors' `distance` for
        the `cosine` metric is `1 - cosine_similarity` (range [0, 2]), and for
        `euclidean` is the raw Euclidean distance -- both already lower-is-
        better, matching cognee's `ScoredResult` contract directly. No
        inversion is applied here (unlike adapters whose backend returns a
        raw higher-is-better similarity score), since S3 Vectors' distance
        value already IS the score.
        """
        sanitized_name = self._sanitize_index_name(collection_name)

        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            logger.warning(
                f"Index '{collection_name}' not found in S3VectorsAdapter.search; returning []."
            )
            return []

        if query_vector is None and query_text:
            query_vector = (await self.embed_data([query_text]))[0]

        # topK is capped at 10,000 per S3 Vectors' documented service limits
        # (100 is only the per-page result count within a paginated
        # QueryVectors response, a different limit -- not implemented as
        # pagination here since cognee's search() contract returns a single
        # flat list, matching every other adapter in this repo).
        limit = max(1, min(limit, 10000)) if limit else 15

        filter_expression = None
        if node_name:
            filter_expression = self._build_node_name_filter(node_name, node_name_filter_operator)

        kwargs = {
            "vectorBucketName": self.vector_bucket_name,
            "indexName": sanitized_name,
            "topK": limit,
            "queryVector": {"float32": [float(v) for v in query_vector]},
            "returnMetadata": True,
            "returnDistance": True,
        }
        if filter_expression is not None:
            kwargs["filter"] = filter_expression

        response = await asyncio.to_thread(self.client.query_vectors, **kwargs)

        scored_results = []
        for vector in response.get("vectors", []):
            payload = self._parse_payload(vector.get("metadata"))
            payload["id"] = parse_id(vector["key"])

            scored_results.append(
                ScoredResult(
                    id=parse_id(vector["key"]),
                    payload=payload,
                    score=vector.get("distance", 0.0),
                )
            )

        return scored_results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
    ) -> list[list[ScoredResult]]:
        """S3 Vectors has no native batch-query endpoint; parallelize
        individual `query_vectors` calls, same approach as other adapters
        without a batch primitive (e.g. azureaisearch)."""
        query_vectors = await self.embed_data(query_texts)

        if limit is None:
            limit = 15

        results = await asyncio.gather(
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

        return results

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        """Delete vectors by key."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        await asyncio.to_thread(
            self.client.delete_vectors,
            vectorBucketName=self.vector_bucket_name,
            indexName=sanitized_name,
            keys=[str(i) for i in data_point_ids],
        )

    async def create_vector_index(self, index_name: str, index_property_name: str):
        """Create a vector index for a specific property."""
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        """Index data points for a specific property."""

        class IndexSchema(DataPoint):
            id: str
            text: str
            metadata: dict = {"index_fields": ["text"]}
            belongs_to_set: list[str] = []

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
        """Delete every vector index in this adapter's vector bucket."""
        try:
            next_token = None
            index_names = []
            while True:
                kwargs = {"vectorBucketName": self.vector_bucket_name}
                if next_token:
                    kwargs["nextToken"] = next_token
                response = await asyncio.to_thread(self.client.list_indexes, **kwargs)
                index_names.extend(index["indexName"] for index in response.get("indexes", []))
                next_token = response.get("nextToken")
                if not next_token:
                    break

            for index_name in index_names:
                try:
                    await asyncio.to_thread(
                        self.client.delete_index,
                        vectorBucketName=self.vector_bucket_name,
                        indexName=index_name,
                    )
                    logger.info(f"Deleted index: {index_name}")
                except ClientError as error:
                    logger.error(f"Error deleting index {index_name}: {str(error)}")
        except ClientError as error:
            logger.error(f"Error during prune operation: {str(error)}")
            raise
