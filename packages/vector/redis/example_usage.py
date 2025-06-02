"""
Example usage of the Redis Vector Store Adapter.

This example demonstrates how to integrate the Redis adapter with Cognee
for vector similarity search operations.
"""

import asyncio
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee_redis_adapter import RedisAdapter


async def main():
    # 1. Initialize your embedding engine (example with OpenAI)
    # You would typically configure this with your actual embedding model
    embedding_engine = EmbeddingEngine(
        model="text-embedding-ada-002",  # or any other model
        # Add your API key and other configuration
    )
    
    # 2. Create the Redis adapter
    redis_adapter = RedisAdapter(
        host="localhost",
        port=6379,
        password=None,  # Set if your Redis requires authentication
        ssl=False,
        embedding_engine=embedding_engine
    )
    
    # 3. Create a collection for your documents
    collection_name = "documents"
    await redis_adapter.create_collection(collection_name)
    
    # 4. Add some sample documents
    from cognee.infrastructure.engine import DataPoint
    from uuid import uuid4
    
    documents = [
        DataPoint(
            id=str(uuid4()),
            text="Cognee is a memory management system for AI applications.",
            metadata={
                "index_fields": ["text"],
                "source": "documentation",
                "type": "intro"
            }
        ),
        DataPoint(
            id=str(uuid4()),
            text="Redis provides high-performance vector search capabilities with RediSearch module.",
            metadata={
                "index_fields": ["text"],
                "source": "technical",
                "type": "database"
            }
        ),
        DataPoint(
            id=str(uuid4()),
            text="Vector databases enable semantic search by finding similar embeddings.",
            metadata={
                "index_fields": ["text"],
                "source": "technical",
                "type": "concept"
            }
        ),
    ]
    
    await redis_adapter.create_data_points(collection_name, documents)
    print(f"Added {len(documents)} documents to the collection")
    
    # 5. Perform a semantic search
    search_query = "What is Cognee used for?"
    results = await redis_adapter.search(
        collection_name=collection_name,
        query_text=search_query,
        limit=5
    )
    
    print(f"\nSearch results for: '{search_query}'")
    for i, result in enumerate(results, 1):
        print(f"{i}. Score: {result.score:.4f}")
        print(f"   Text: {result.payload.get('text', '')}")
        print(f"   Source: {result.payload.get('metadata', {}).get('source', 'N/A')}")
        print()
    
    # 6. Example of batch search
    queries = [
        "How does Redis handle vectors?",
        "What is semantic search?"
    ]
    
    batch_results = await redis_adapter.batch_search(
        collection_name=collection_name,
        query_texts=queries,
        limit=3
    )
    
    print("\nBatch search results:")
    for query, results in zip(queries, batch_results):
        print(f"\nQuery: '{query}'")
        for result in results:
            print(f"  - {result.payload.get('text', '')[:60]}... (score: {result.score:.4f})")
    
    # 7. Clean up (optional)
    # await redis_adapter.prune()  # This will remove all collections


if __name__ == "__main__":
    asyncio.run(main()) 