import json
import numpy as np
from typing import Dict, List, Optional, Any
from uuid import UUID
from redis import asyncio as aioredis
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search import index_definition
from redis.commands.search.query import Query

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


def serialize_for_json(obj):
    """Convert objects to JSON-serializable format."""
    if isinstance(obj, UUID):
        return str(obj)
    elif isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_for_json(item) for item in obj]
    else:
        return obj


class IndexSchema(DataPoint):
    text: str

    metadata: dict = {"index_fields": ["text"]}


class RedisAdapter(VectorDBInterface):
    name = "Redis"
    host: str = None
    port: int = None
    password: str = None
    ssl: bool = False
    embedding_engine: EmbeddingEngine = None
    
    def __init__(
        self, 
        host: str,
        port: int = 6379,
        password: Optional[str] = None,
        ssl: bool = False,
        embedding_engine: EmbeddingEngine = None
    ):
        if not (host and embedding_engine):
            raise VectorEngineInitializationError("Missing required Redis credentials!")
        
        self.host = host
        self.port = port
        self.password = password
        self.ssl = ssl
        self.embedding_engine = embedding_engine
    
    def get_redis_client(self) -> aioredis.Redis:
        """Get an async Redis client instance."""
        return aioredis.Redis(
            host=self.host,
            port=self.port,
            password=self.password,
            ssl=self.ssl,
            decode_responses=True
        )
    
    async def embed_data(self, data: List[str]) -> List[List[float]]:
        """Embed text data using the embedding engine."""
        return await self.embedding_engine.embed_text(data)
    
    def _get_index_name(self, collection_name: str) -> str:
        """Get the Redis index name for a collection."""
        return f"idx:{collection_name}"
    
    def _get_key_prefix(self, collection_name: str) -> str:
        """Get the Redis key prefix for a collection."""
        return f"{collection_name}:"
    
    async def has_collection(self, collection_name: str) -> bool:
        """Check if a collection (index) exists."""
        client = self.get_redis_client()
        try:
            index_name = self._get_index_name(collection_name)
            # Try to get index info - will throw if doesn't exist
            await client.ft(index_name).info()
            return True
        except Exception:
            return False
        finally:
            await client.close()
    
    async def create_collection(
        self,
        collection_name: str,
        payload_schema=None,
    ):
        """Create a new collection (Redis index) with vector search capabilities."""
        client = self.get_redis_client()
        try:
            if await self.has_collection(collection_name):
                logger.info(f"Collection {collection_name} already exists")
                return
            
            index_name = self._get_index_name(collection_name)
            prefix = self._get_key_prefix(collection_name)
            
            # Define the schema for the index
            schema = [
                TextField("$.id", as_name="id"),
                TextField("$.text", as_name="text"),
                VectorField(
                    "$.vector",
                    "FLAT",  # Can also use "HNSW" for approximate search
                    {
                        "TYPE": "FLOAT32",
                        "DIM": self.embedding_engine.get_vector_size(),
                        "DISTANCE_METRIC": "COSINE"
                    },
                    as_name="vector"
                ),
                # Store the entire payload as JSON
                TextField("$.payload", as_name="payload")
            ]
            
            # Create index definition
            definition = index_definition.IndexDefinition(
                prefix=[prefix],
                index_type=index_definition.IndexType.JSON
            )
            
            # Create the index
            await client.ft(index_name).create_index(
                fields=schema,
                definition=definition
            )
            
            logger.info(f"Created collection {collection_name}")
            
        except Exception as e:
            logger.error(f"Error creating collection {collection_name}: {str(e)}")
            raise e
        finally:
            await client.close()
    
    async def create_data_points(self, collection_name: str, data_points: List[DataPoint]):
        """Create data points in the collection."""
        client = self.get_redis_client()
        try:
            if not await self.has_collection(collection_name):
                raise CollectionNotFoundError(f"Collection {collection_name} not found!")
            
            # Embed the data points - use the same pattern as Qdrant
            data_vectors = await self.embed_data(
                [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
            )
            
            # Store each data point
            pipe = client.pipeline()
            for data_point, embedding in zip(data_points, data_vectors):
                key = f"{self._get_key_prefix(collection_name)}{str(data_point.id)}"
                
                # Convert data point to dict and prepare for storage
                # Serialize the payload to handle UUIDs and other non-JSON types
                payload = serialize_for_json(data_point.model_dump())
                
                doc_data = {
                    "id": str(data_point.id),
                    "text": getattr(data_point, data_point.metadata.get("index_fields", ["text"])[0], ""),
                    "vector": embedding,
                    "payload": payload
                }
                
                # Store as JSON
                pipe.json().set(key, "$", doc_data)
            
            await pipe.execute()
            logger.info(f"Created {len(data_points)} data points in collection {collection_name}")
            
        except Exception as e:
            logger.error(f"Error creating data points: {str(e)}")
            raise e
        finally:
            await client.close()
    
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
                    id=data_point.id,
                    text=getattr(data_point, data_point.metadata["index_fields"][0]),
                )
                for data_point in data_points
            ],
        )
    
    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        """Retrieve data points by their IDs."""
        client = self.get_redis_client()
        try:
            results = []
            for data_id in data_point_ids:
                key = f"{self._get_key_prefix(collection_name)}{data_id}"
                data = await client.json().get(key)
                if data:
                    # Return the payload which contains the full DataPoint data
                    results.append(data["payload"])
            return results
        finally:
            await client.close()
    
    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        limit: int = 15,
        with_vector: bool = False,
    ) -> List[ScoredResult]:
        """Search for similar vectors in the collection."""
        if query_text is None and query_vector is None:
            raise InvalidValueError("One of query_text or query_vector must be provided!")
        
        if limit <= 0:
            return []
        
        if not await self.has_collection(collection_name):
            logger.warning(f"Collection '{collection_name}' not found in RedisAdapter.search; returning [].")
            return []
        
        client = self.get_redis_client()
        try:
            # Get the query vector - match Qdrant pattern
            if query_vector is None:
                query_vector = (await self.embed_data([query_text]))[0]
            
            # Convert to bytes for Redis
            query_vector_bytes = np.array(query_vector, dtype=np.float32).tobytes()
            
            # Build the query
            index_name = self._get_index_name(collection_name)
            q = Query(f"*=>[KNN {limit} @vector $vec AS score]") \
                .sort_by("score") \
                .return_fields("id", "text", "payload", "score") \
                .dialect(2)
            
            if with_vector:
                q.return_fields("vector")
            
            # Execute the search
            results = await client.ft(index_name).search(
                q,
                query_params={"vec": query_vector_bytes}
            )
            
            # Convert results to ScoredResult objects
            scored_results = []
            for doc in results.docs:
                # Parse the stored payload - it's already serialized as JSON-compatible format
                if isinstance(doc.payload, str):
                    payload = json.loads(doc.payload)
                else:
                    payload = doc.payload
                
                scored_results.append(
                    ScoredResult(
                        id=parse_id(doc.id.split(":")[-1]),  # Extract ID from Redis key
                        payload=payload,
                        score=float(doc.score)  # Redis returns distance, not similarity
                    )
                )
            
            return scored_results
            
        except Exception as e:
            logger.error(f"Error during search: {str(e)}")
            raise e
        finally:
            await client.close()
    
    async def batch_search(
        self,
        collection_name: str,
        query_texts: List[str],
        limit: int = None,
        with_vectors: bool = False,
    ):
        """Perform batch search for multiple queries."""
        # Redis doesn't have native batch search, so we'll execute searches in parallel
        import asyncio
        
        # Embed all queries at once - match Qdrant pattern
        vectors = await self.embed_data(query_texts)
        
        # Execute searches in parallel
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
        
        # Filter results by score threshold (similar to Qdrant adapter)
        return [
            [result for result in result_group if result.score < 0.1]  # Redis uses distance, so lower is better
            for result_group in results
        ]
    
    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
        """Delete data points by their IDs."""
        client = self.get_redis_client()
        try:
            pipe = client.pipeline()
            for data_id in data_point_ids:
                key = f"{self._get_key_prefix(collection_name)}{data_id}"
                pipe.delete(key)
            
            results = await pipe.execute()
            deleted_count = sum(1 for result in results if result == 1)
            logger.info(f"Deleted {deleted_count} data points from collection {collection_name}")
            return {"deleted": deleted_count}
            
        except Exception as e:
            logger.error(f"Error deleting data points: {str(e)}")
            raise e
        finally:
            await client.close()
    
    async def prune(self):
        """Remove all collections and data from Redis."""
        client = self.get_redis_client()
        try:
            # Get all index names
            indices = await client.execute_command("FT._LIST")
            
            # Drop each index
            for index in indices:
                try:
                    await client.ft(index).dropindex(delete_documents=True)
                    logger.info(f"Dropped index {index}")
                except Exception as e:
                    logger.warning(f"Failed to drop index {index}: {str(e)}")
            
            logger.info("Pruned all Redis vector collections")
            
        except Exception as e:
            logger.error(f"Error during prune: {str(e)}")
            raise e
        finally:
            await client.close() 