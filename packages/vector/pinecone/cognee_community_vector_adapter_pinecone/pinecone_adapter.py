import asyncio
from typing import List, Optional

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
from pinecone import Pinecone, ServerlessSpec

logger = get_logger("PineconeAdapter")


def sanitize_pinecone_name(name: str) -> str:
    """
    Sanitize a name to comply with Pinecone's naming requirements:
    - Only lowercase alphanumeric characters and hyphens
    - Must start with a letter
    - Must not end with a hyphen
    """
    # Convert to lowercase
    name = name.lower()

    # Replace invalid characters with hyphens
    import re

    name = re.sub(r"[^a-z0-9\-]", "-", name)

    # Ensure it starts with a letter
    if not name[0].isalpha():
        name = "index-" + name

    # Remove consecutive hyphens
    name = re.sub(r"-+", "-", name)

    # Ensure it doesn't end with a hyphen
    name = name.rstrip("-")

    return name


class IndexSchema(DataPoint):
    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: list[str] = []


class PineconeAdapter(VectorDBInterface):
    name = "Pinecone"
    api_key: str = None
    environment: str = None
    cloud: str = None
    region: str = None

    def __init__(
        self,
        url,
        api_key,
        embedding_engine: EmbeddingEngine,
        database_name: str = "cognee",
        environment: str = None,
        cloud: str = None,
        region: str = None,
    ):
        self.url = url  # Not used by Pinecone, but required by Cognee interface
        self.api_key = api_key
        self.environment = environment
        self.database_name = database_name
        self.cloud = cloud if cloud is not None else "aws"
        self.region = region if region is not None else "us-east-1"
        self.embedding_engine = embedding_engine
        self.VECTOR_DB_LOCK = asyncio.Lock()

        # Initialize Pinecone client
        self.pc = Pinecone(api_key=api_key)

    def get_pinecone_index(self, collection_name: str):
        """Get Pinecone index instance"""
        return self.pc.Index(collection_name)

    async def embed_data(self, data: list[str]) -> list[float]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        try:
            index_list = self.pc.list_indexes()
            return collection_name in [index.name for index in index_list]
        except Exception as e:
            logger.error("Error checking if collection exists: %s", str(e))
            return False

    async def create_collection(
        self,
        collection_name: str,
        payload_schema=None,
    ):
        async with self.VECTOR_DB_LOCK:
            if not await self.has_collection(collection_name):
                try:
                    self.pc.create_index(
                        name=collection_name,
                        dimension=self.embedding_engine.get_vector_size(),
                        metric="cosine",
                        spec=ServerlessSpec(cloud=self.cloud, region=self.region),
                    )
                    logger.info("Created Pinecone index: %s", collection_name)
                except Exception as e:
                    logger.error("Error creating collection: %s", str(e))
                    raise e

    async def create_data_points(
        self, collection_name: str, data_points: list[DataPoint]
    ):
        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(
                    message=f"Collection {collection_name} not found!"
                )

            index = self.get_pinecone_index(collection_name)

            data_vectors = await self.embed_data(
                [
                    DataPoint.get_embeddable_data(data_point)
                    for data_point in data_points
                ]
            )

            def convert_to_pinecone_vector(data_point: DataPoint):
                # Clean metadata to only include Pinecone-compatible types
                clean_metadata = {}
                data_dump = data_point.model_dump()

                for key, value in data_dump.items():
                    if (
                        key != "metadata"
                    ):  # Skip the nested metadata field that causes issues
                        if isinstance(value, (str, int, float, bool)):
                            clean_metadata[key] = value
                        elif isinstance(value, list) and all(
                            isinstance(item, str) for item in value
                        ):
                            clean_metadata[key] = value
                        else:
                            # Convert complex types to strings
                            clean_metadata[key] = str(value)

                return {
                    "id": str(data_point.id),
                    "values": data_vectors[data_points.index(data_point)],
                    "metadata": clean_metadata,
                }

            vectors = [convert_to_pinecone_vector(point) for point in data_points]

            index.upsert(vectors=vectors)
            logger.info("Uploaded %d data points to Pinecone", len(vectors))

        except Exception as error:
            logger.error("Error uploading data points to Pinecone: %s", str(error))
            raise error

    async def create_vector_index(self, index_name: str, index_property_name: str):
        sanitized_name = sanitize_pinecone_name(f"{index_name}_{index_property_name}")
        await self.create_collection(sanitized_name)

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ):
        sanitized_name = sanitize_pinecone_name(f"{index_name}_{index_property_name}")
        await self.create_data_points(
            sanitized_name,
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
        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(
                    message=f"Collection {collection_name} not found!"
                )

            index = self.get_pinecone_index(collection_name)
            results = index.fetch(ids=data_point_ids)
            return results
        except Exception as e:
            logger.error("Error retrieving data points: %s", str(e))
            raise e

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int = 15,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
        **kwargs: object,
    ) -> list[ScoredResult]:
        """Search for similar vectors in the collection.

        Args:
            collection_name: Name of the collection to search.
            query_text: Text query to search for (will be embedded).
            query_vector: Pre-computed query vector.
            limit: Maximum number of results to return.
            with_vector: Whether to include vectors in results.
            include_payload: Whether to include payload in results.

        Returns:
            List of ScoredResult objects sorted by similarity.

        Raises:
            MissingQueryParameterError: If neither query_text nor query_vector is provided.
        """
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            logger.warning(
                f"Collection '{collection_name}' not found in PineconeAdapter.search; returning []."
            )
            return []

        if query_vector is None:
            query_vector = (await self.embed_data([query_text]))[0]

        try:
            index = self.get_pinecone_index(collection_name)

            if limit == 0:
                # Get actual index stats instead of hardcoded limit
                stats = index.describe_index_stats()
                limit = stats.total_vector_count

            if limit == 0:
                return []

            filter_params = kwargs.get("filter", {})
            if node_name:
                if node_name_filter_operator == "AND":
                    filter_params["$and"] = [
                        {"belongs_to_set": {"$in": [name]}} for name in node_name
                    ]
                else:
                    filter_params["belongs_to_set"] = {"$in": node_name}

            query_kwargs = {
                "vector": query_vector,
                "top_k": limit,
                "include_metadata": include_payload,
                "include_values": with_vector,
            }
            if filter_params:
                query_kwargs["filter"] = filter_params

            results = index.query(**query_kwargs)

            return [
                ScoredResult(
                    id=parse_id(match.id),
                    payload=(
                        {
                            **match.metadata,
                            "id": parse_id(match.id),
                        }
                        if include_payload
                        else {}
                    ),
                    # Pinecone returns similarity score (0-1, higher = more similar)
                    score=match.score,
                )
                for match in results.matches
            ]

        except Exception as e:
            logger.error("Error searching collection: %s", str(e))
            raise e

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
        **kwargs: object,
    ) -> list[list[ScoredResult]]:
        """Perform batch search for multiple queries.

        Args:
            collection_name: Name of the collection to search.
            query_texts: List of query texts to search for.
            limit: Maximum number of results per query.
            with_vectors: Whether to include vectors in results.
            include_payload: Whether to include payload in results.

        Returns:
            List of search results, one list per query.

        Raises:
            Exception: If search execution fails.
        """
        if limit is None:
            limit = 15

        if not await self.has_collection(collection_name):
            logger.warning(
                f"Collection '{collection_name}' not found in PineconeAdapter.batch_search;"
                f"returning empty results."
            )
            return [[] for _ in query_texts]

        try:
            vectors = await self.embed_data(query_texts)
            index = self.get_pinecone_index(collection_name)

            results = []
            for i, vector in enumerate(vectors):
                try:
                    filter_params = kwargs.get("filter", {})
                    if node_name:
                        if node_name_filter_operator == "AND":
                            filter_params["$and"] = [
                                {"belongs_to_set": {"$in": [name]}}
                                for name in node_name
                            ]
                        else:
                            filter_params["belongs_to_set"] = {"$in": node_name}

                    query_kwargs = {
                        "vector": vector,
                        "top_k": limit,
                        "include_metadata": include_payload,
                        "include_values": with_vectors,
                    }
                    if filter_params:
                        query_kwargs["filter"] = filter_params

                    result = index.query(**query_kwargs)

                    # Convert to ScoredResult objects (no filtering to match other adapters)
                    scored_results = [
                        ScoredResult(
                            id=parse_id(match.id),
                            payload=(
                                {
                                    **match.metadata,
                                    "id": parse_id(match.id),
                                }
                                if include_payload
                                else {}
                            ),
                            score=match.score,
                        )
                        for match in result.matches
                    ]
                    results.append(scored_results)

                except Exception as e:
                    logger.error(f"Error in batch search for query {i}: {str(e)}")
                    results.append([])

            return results

        except Exception as e:
            logger.error(f"Error during batch search: {str(e)}")
            raise e

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(
                    message=f"Collection {collection_name} not found!"
                )

            index = self.get_pinecone_index(collection_name)
            results = index.delete(ids=data_point_ids)
            logger.info(
                "Deleted %d data points from %s", len(data_point_ids), collection_name
            )
            return results
        except Exception as e:
            logger.error("Error deleting data points: %s", str(e))
            raise e

    async def prune(self):
        """Delete all indexes in Pinecone"""
        try:
            index_list = self.pc.list_indexes()

            for index_info in index_list:
                self.pc.delete_index(index_info.name)
                logger.info("Deleted Pinecone index: %s", index_info.name)

        except Exception as e:
            logger.error("Error pruning Pinecone indexes: %s", str(e))
            raise e
