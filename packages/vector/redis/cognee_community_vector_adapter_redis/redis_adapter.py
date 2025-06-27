import json
import asyncio
from typing import Dict, List, Optional, Any
from uuid import UUID

from redisvl.index import AsyncSearchIndex
from redisvl.schema import IndexSchema
from redisvl.query import VectorQuery

from cognee.exceptions import InvalidValueError
from cognee.shared.logging_utils import get_logger

from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine

logger = get_logger("RedisAdapter")


class VectorEngineInitializationError(Exception):
    """Exception raised when vector engine initialization fails."""
    pass


class CollectionNotFoundError(Exception):
    """Exception raised when a collection is not found."""
    pass


def serialize_for_json(obj: Any) -> Any:
    """Convert objects to JSON-serializable format.
    
    Args:
        obj: Object to serialize (UUID, dict, list, or any other type).
        
    Returns:
        JSON-serializable representation of the object.
    """
    if isinstance(obj, UUID):
        return str(obj)
    elif isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    else:
        return obj


class RedisDataPoint(DataPoint):
    """Redis data point schema for vector index entries.
    
    Attributes:
        text: The text content to be indexed.
        metadata: Metadata containing index field configuration.
    """
    text: str
    metadata: dict = {"index_fields": ["text"]}


class RedisAdapter(VectorDBInterface):
    """Redis vector database adapter using RedisVL for vector similarity search.
    
    This adapter provides an interface to Redis vector search capabilities,
    supporting embedding generation, vector indexing, and similarity search.
    """
    
    name = "Redis"
    url: Optional[str]
    api_key: Optional[str] = None
    embedding_engine: Optional[EmbeddingEngine] = None
    
    def __init__(
        self, 
        url: str,
        api_key: Optional[str] = None,
        embedding_engine: Optional[EmbeddingEngine] = None
    ) -> None:
        """Initialize the Redis adapter.
        
        Args:
            url (str): Connection string for your Redis instance like redis://localhost:6379.
            embedding_engine: Engine for generating embeddings.
            api_key: Optional API key. Ignored for Redis.
            
        Raises:
            VectorEngineInitializationError: If required parameters are missing.
        """
        if not url:
            raise VectorEngineInitializationError("Redis connnection URL!")
        if not embedding_engine:
            raise VectorEngineInitializationError("Embedding engine is required!")
        
        self.url = url
        self.embedding_engine = embedding_engine
        self._indices = {}
        
    async def embed_data(self, data: List[str]) -> List[List[float]]:
        """Embed text data using the embedding engine.
        
        Args:
            data: List of text strings to embed.
            
        Returns:
            List of embedding vectors as lists of floats.
            
        Raises:
            Exception: If embedding generation fails.
        """
        return await self.embedding_engine.embed_text(data)
    
    def _create_schema(self, collection_name: str) -> IndexSchema:
        """Create a RedisVL IndexSchema for a collection.
        
        Args:
            collection_name: Name of the collection to create an index schema for.
            
        Returns:
            Redis IndexSchema configured for vector search.
        """
        schema_dict = {
            "index": {
                "name": collection_name,
                "prefix": f"{collection_name}",
                "storage_type": "json"
            },
            "fields": [
                {"name": "id", "type": "tag", "attrs": {"sortable": True}},
                {"name": "text", "type": "text", "attrs": {"sortable": True}},
                {
                    "name": "vector",
                    "type": "vector",
                    "attrs": {
                        "algorithm": "hnsw",
                        "m": 32,
                        "dims": self.embedding_engine.get_vector_size(),
                        "distance_metric": "cosine",
                        "datatype": "float32"
                    }
                },
                {"name": "payload", "type": "text", "attrs": {"sortable": True}}
            ]
        }
        return IndexSchema.from_dict(schema_dict)
    
    def _get_index(self, collection_name: str) -> AsyncSearchIndex:
        """Get or create an AsyncSearchIndex for a collection.
        
        Args:
            collection_name: Name of the collection.
            
        Returns:
            AsyncSearchIndex instance for the collection.
        """
        if collection_name not in self._indices:
            schema = self._create_schema(collection_name)
            self._indices[collection_name] = AsyncSearchIndex(
                schema=schema,
                redis_url=self.url,
                validate_on_load=True
            )
        return self._indices[collection_name]
    
    async def has_collection(self, collection_name: str) -> bool:
        """Check if a collection (index) exists.
        
        Args:
            collection_name: Name of the collection to check.
            
        Returns:
            True if collection exists, False otherwise.
        """
        try:
            index = self._get_index(collection_name)
            return await index.exists()
        except Exception:
            return False
    
    async def create_collection(
        self,
        collection_name: str,
        payload_schema: Optional[Any] = None,
    ) -> None:
        """Create a new collection (Redis index) with vector search capabilities.
        
        Args:
            collection_name: Name of the collection to create.
            payload_schema: Schema for payload data (not used).
            
        Raises:
            Exception: If collection creation fails.
        """
        try:
            if await self.has_collection(collection_name):
                logger.info(f"Collection {collection_name} already exists")
                return
            
            index = self._get_index(collection_name)
            await index.create(overwrite=False)
            
            logger.info(f"Created collection {collection_name}")
            
        except Exception as e:
            logger.error(f"Error creating collection {collection_name}: {str(e)}")
            raise e
    
    async def create_data_points(self, collection_name: str, data_points: List[DataPoint]) -> None:
        """Create data points in the collection.
        
        Args:
            collection_name: Name of the target collection.
            data_points: List of DataPoint objects to insert.
            
        Raises:
            CollectionNotFoundError: If the collection doesn't exist.
            Exception: If data point creation fails.
        """
        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(f"Collection {collection_name} not found!")
            
            # Embed the data points
            data_vectors = await self.embed_data(
                [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
            )
            
            # Prepare documents for RedisVL
            documents = []
            for data_point, embedding in zip(data_points, data_vectors):
                # Serialize the payload to handle UUIDs and other non-JSON types
                payload = serialize_for_json(data_point.model_dump())
                
                doc_data = {
                    "id": str(data_point.id),
                    "text": getattr(data_point, data_point.metadata.get("index_fields", ["text"])[0], ""),
                    "vector": embedding,
                    "payload": json.dumps(payload)  # Store as JSON string
                }
                documents.append(doc_data)
            
            # Load using RedisVL
            index = self._get_index(collection_name)
            await index.load(documents, id_field="id")
            
            logger.info(f"Created {len(data_points)} data points in collection {collection_name}")
            
        except Exception as e:
            logger.error(f"Error creating data points: {str(e)}")
            raise e
    
    async def create_vector_index(self, index_name: str, index_property_name: str) -> None:
        """Create a vector index for a specific property.
        
        Args:
            index_name: Base name for the index.
            index_property_name: Property name to index.
        """
        await self.create_collection(f"{index_name}_{index_property_name}")
    
    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ) -> None:
        """Index data points for a specific property.
        
        Args:
            index_name: Base name for the index.
            index_property_name: Property name to index.
            data_points: List of DataPoint objects to index.
        """
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                RedisDataPoint(
                    id=data_point.id,
                    text=getattr(data_point, data_point.metadata.get("index_fields", ["text"])[0]),
                )
                for data_point in data_points
            ],
        )
    
    async def retrieve(self, collection_name: str, data_point_ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve data points by their IDs.
        
        Args:
            collection_name: Name of the collection to retrieve from.
            data_point_ids: List of data point IDs to retrieve.
            
        Returns:
            List of retrieved data point payloads.
        """
        try:
            index = self._get_index(collection_name)
            results = []
            
            for data_id in data_point_ids:
                doc = await index.fetch(data_id)
                if doc:
                    # Parse the stored payload JSON
                    payload_str = doc.get("payload", "{}")
                    try:
                        payload = json.loads(payload_str)
                        results.append(payload)
                    except json.JSONDecodeError:
                        # Fallback to the document itself if payload parsing fails
                        results.append(doc)
            
            return results
            
        except Exception as e:
            logger.error(f"Error retrieving data points: {str(e)}")
            return []
    
    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        limit: int = 15,
        with_vector: bool = False,
    ) -> List[ScoredResult]:
        """Search for similar vectors in the collection.
        
        Args:
            collection_name: Name of the collection to search.
            query_text: Text query to search for (will be embedded).
            query_vector: Pre-computed query vector.
            limit: Maximum number of results to return.
            with_vector: Whether to include vectors in results.
            
        Returns:
            List of ScoredResult objects sorted by similarity.
            
        Raises:
            InvalidValueError: If neither query_text nor query_vector is provided.
            Exception: If search execution fails.
        """
        if query_text is None and query_vector is None:
            raise InvalidValueError("One of query_text or query_vector must be provided!")
        
        if limit <= 0:
            return []
        
        if not await self.has_collection(collection_name):
            logger.warning(f"Collection '{collection_name}' not found in RedisAdapter.search; returning [].")
            return []
        
        try:
            # Get the query vector
            if query_vector is None:
                query_vector = (await self.embed_data([query_text]))[0]
            
            # Create the vector query
            vector_query = VectorQuery(
                vector=query_vector,
                vector_field_name="vector",
                num_results=limit,
                return_score=True,
                normalize_vector_distance=True
            )
            
            # Set return fields
            return_fields = ["id", "text", "payload"]
            if with_vector:
                return_fields.append("vector")
            vector_query = vector_query.return_fields(*return_fields)
            
            # Execute the search
            index = self._get_index(collection_name)
            results = await index.query(vector_query)
            
            # Convert results to ScoredResult objects
            scored_results = []
            for doc in results:
                # Parse the stored payload - it's stored as JSON string
                payload_str = doc.get("payload", "{}")
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    payload = doc
                
                scored_results.append(
                    ScoredResult(
                        id=parse_id(doc["id"]),
                        payload=payload,
                        score=float(doc.get("vector_distance", 0.0))  # RedisVL returns distance
                    )
                )
            return scored_results
            
        except Exception as e:
            logger.error(f"Error during search: {str(e)}")
            raise e
    
    async def batch_search(
        self,
        collection_name: str,
        query_texts: List[str],
        limit: Optional[int] = None,
        with_vectors: bool = False,
    ) -> List[List[ScoredResult]]:
        """Perform batch search for multiple queries.
        
        Args:
            collection_name: Name of the collection to search.
            query_texts: List of text queries to search for.
            limit: Maximum number of results per query.
            with_vectors: Whether to include vectors in results.
            
        Returns:
            List of search results for each query, filtered by score threshold.
        """
        # Embed all queries at once
        vectors = await self.embed_data(query_texts)
        
        # Execute searches in parallel
        # TODO: replace with index.batch_query() in the future
        search_tasks = [
            self.search(
                collection_name=collection_name,
                query_vector=vector,
                limit=limit,
                with_vector=with_vectors
            )
            for vector in vectors
        ]
        
        results = await asyncio.gather(*search_tasks)
        
        # Filter results by score threshold (Redis uses distance, so lower is better)
        return [
            [result for result in result_group if result.score < 0.1]
            for result_group in results
        ]
    
    async def delete_data_points(self, collection_name: str, data_point_ids: List[str]) -> Dict[str, int]:
        """Delete data points by their IDs.
        
        Args:
            collection_name: Name of the collection to delete from.
            data_point_ids: List of data point IDs to delete.
            
        Returns:
            Dictionary containing the number of deleted documents.
            
        Raises:
            Exception: If deletion fails.
        """
        try:
            index = self._get_index(collection_name)
            deleted_count = await index.drop_documents(data_point_ids)
            logger.info(f"Deleted {deleted_count} data points from collection {collection_name}")
            return {"deleted": deleted_count}
        except Exception as e:
            logger.error(f"Error deleting data points: {str(e)}")
            raise e
    
    async def prune(self) -> None:
        """Remove all collections and data from Redis.
        
        This method drops all existing indices and clears the internal cache.
        
        Raises:
            Exception: If pruning fails.
        """
        try:
            # Get all existing indices and delete them
            for collection_name, index in self._indices.items():
                try:
                    if await index.exists():
                        await index.delete(drop=True)
                        logger.info(f"Dropped index {collection_name}")
                        await index.disconnect()
                except Exception as e:
                    logger.warning(f"Failed to drop index {collection_name}: {str(e)}")
            
            # Clear the indices cache
            self._indices.clear()
            
            logger.info("Pruned all Redis vector collections")
            
        except Exception as e:
            logger.error(f"Error during prune: {str(e)}")
            raise e
