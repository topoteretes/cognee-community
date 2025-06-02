"""
Test file for Redis Vector Store Adapter.

This file demonstrates how to use the Redis adapter and can be used
for testing the implementation.
"""

import asyncio
from uuid import uuid4
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from redis_adapter import RedisAdapter


# Mock embedding engine for testing
class MockEmbeddingEngine(EmbeddingEngine):
    def __init__(self):
        self.vector_size = 384  # Common size for sentence transformers
    
    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        """Create mock embeddings for testing."""
        import random
        return [[random.random() for _ in range(self.vector_size)] for _ in texts]
    
    def get_vector_size(self) -> int:
        return self.vector_size


async def test_redis_adapter():
    """Test the Redis adapter functionality."""
    
    # Initialize the adapter
    embedding_engine = MockEmbeddingEngine()
    adapter = RedisAdapter(
        host="localhost",
        port=6379,
        embedding_engine=embedding_engine
    )
    
    collection_name = "test_collection"
    
    try:
        # Test 1: Create collection
        print("1. Creating collection...")
        await adapter.create_collection(collection_name)
        
        # Test 2: Check if collection exists
        print("2. Checking if collection exists...")
        exists = await adapter.has_collection(collection_name)
        assert exists, "Collection should exist after creation"
        print(f"   Collection exists: {exists}")
        
        # Test 3: Create data points
        print("3. Creating data points...")
        data_points = [
            DataPoint(
                id=str(uuid4()),
                text="The quick brown fox jumps over the lazy dog",
                metadata={"index_fields": ["text"], "category": "animals"}
            ),
            DataPoint(
                id=str(uuid4()),
                text="Redis is an in-memory data structure store",
                metadata={"index_fields": ["text"], "category": "technology"}
            ),
            DataPoint(
                id=str(uuid4()),
                text="Vector databases enable similarity search",
                metadata={"index_fields": ["text"], "category": "technology"}
            ),
        ]
        await adapter.create_data_points(collection_name, data_points)
        print(f"   Created {len(data_points)} data points")
        
        # Test 4: Search functionality
        print("4. Testing search...")
        results = await adapter.search(
            collection_name=collection_name,
            query_text="Redis database",
            limit=2
        )
        print(f"   Found {len(results)} results")
        for i, result in enumerate(results):
            print(f"   Result {i+1}: Score={result.score:.4f}, Text={result.payload.get('text', '')[:50]}...")
        
        # Test 5: Batch search
        print("5. Testing batch search...")
        batch_results = await adapter.batch_search(
            collection_name=collection_name,
            query_texts=["fox animal", "redis memory"],
            limit=2
        )
        print(f"   Batch search returned {len(batch_results)} result groups")
        
        # Test 6: Retrieve by IDs
        print("6. Testing retrieve by IDs...")
        ids_to_retrieve = [str(dp.id) for dp in data_points[:2]]
        retrieved = await adapter.retrieve(collection_name, ids_to_retrieve)
        print(f"   Retrieved {len(retrieved)} data points")
        
        # Test 7: Delete data points
        print("7. Testing delete...")
        delete_ids = [str(data_points[0].id)]
        delete_result = await adapter.delete_data_points(collection_name, delete_ids)
        print(f"   Deleted {delete_result.get('deleted', 0)} data points")
        
        # Test 8: Prune all collections
        print("8. Testing prune...")
        await adapter.prune()
        print("   All collections pruned")
        
        # Verify collection is gone
        exists_after_prune = await adapter.has_collection(collection_name)
        assert not exists_after_prune, "Collection should not exist after prune"
        print(f"   Collection exists after prune: {exists_after_prune}")
        
        print("\n✅ All tests passed!")
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        raise


if __name__ == "__main__":
    # Run the test
    asyncio.run(test_redis_adapter()) 