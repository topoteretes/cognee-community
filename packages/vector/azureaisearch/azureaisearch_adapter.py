import asyncio
from typing import Dict, List, Optional
from azure.search.documents import SearchClient
from azure.search.documents.aio import SearchClient as AsyncSearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SearchableField,
    SimpleField,
)
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError

from cognee.exceptions import InvalidValueError
from cognee.shared.logging_utils import get_logger
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.modules.storage.utils import get_own_properties
from cognee.infrastructure.databases.vector import VectorDBInterface
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError,
)

logger = get_logger("AzureAISearchAdapter")


class IndexSchema(DataPoint):
    id: str
    text: str
    metadata: dict = {"index_fields": ["text"]}


class AzureAISearchAdapter(VectorDBInterface):
    name = "AzureAISearch"
    endpoint: str = None
    api_key: str = None
    embedding_engine: EmbeddingEngine = None
    index_client: SearchIndexClient = None

    def __init__(
            self,
            url: Optional[str] = None,
            api_key: Optional[str] = None,
            embedding_engine: EmbeddingEngine = None,
            endpoint: Optional[str] = None,
            **kwargs  # Accept additional keyword arguments
    ):
        # Handle both 'url' and 'endpoint' parameters
        # Also handle the 'utl' typo that appears to be in cognee
        final_endpoint = endpoint or url or kwargs.get('utl')

        if not (final_endpoint and api_key and embedding_engine):
            raise ValueError("Missing required Azure AI Search credentials!")

        self.endpoint = final_endpoint
        self.api_key = api_key
        self.embedding_engine = embedding_engine
        self.credential = AzureKeyCredential(api_key)
        self.index_client = SearchIndexClient(endpoint=self.endpoint, credential=self.credential)

    def _sanitize_index_name(self, name: str) -> str:
        """
        Sanitize index name to meet Azure AI Search requirements:
        - Only lowercase letters, digits, or dashes
        - Cannot start or end with dashes
        - Limited to 128 characters
        """
        import re

        # Convert to lowercase and replace invalid characters with dashes
        sanitized = re.sub(r'[^a-z0-9-]', '-', name.lower())

        # Replace multiple consecutive dashes with a single dash
        sanitized = re.sub(r'-+', '-', sanitized)

        # Remove leading and trailing dashes
        sanitized = sanitized.strip('-')

        # Ensure it doesn't start with a number (add prefix if needed)
        if sanitized and sanitized[0].isdigit():
            sanitized = 'idx-' + sanitized

        # Truncate to 128 characters if necessary
        if len(sanitized) > 128:
            sanitized = sanitized[:128].rstrip('-')

        # If empty after sanitization, use a default name
        if not sanitized:
            sanitized = 'default-index'

        return sanitized

    def get_search_client(self, index_name: str) -> SearchClient:
        """Get a synchronous search client for the specified index."""
        return SearchClient(
            endpoint=self.endpoint,
            index_name=index_name,
            credential=self.credential,
        )

    def get_async_search_client(self, index_name: str) -> AsyncSearchClient:
        """Get an asynchronous search client for the specified index."""
        return AsyncSearchClient(
            endpoint=self.endpoint,
            index_name=index_name,
            credential=self.credential,
        )

    async def embed_data(self, data: List[str]) -> List[List[float]]:
        return await self.embedding_engine.embed_text(data)

    async def has_collection(self, collection_name: str) -> bool:
        """Check if an index exists (collection in Azure AI Search is an index)."""
        try:
            sanitized_name = self._sanitize_index_name(collection_name)
            self.index_client.get_index(sanitized_name)
            return True
        except ResourceNotFoundError:
            return False

    async def create_collection(self, collection_name: str, payload_schema=None):
        """Create a new search index with vector search configuration."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if await self.has_collection(collection_name):
            return

        vector_size = self.embedding_engine.get_vector_size()

        # Define the fields for the index
        fields = [
            SimpleField(
                name="id",
                type=SearchFieldDataType.String,
                key=True,
                filterable=True,
            ),
            SearchableField(
                name="text",
                type=SearchFieldDataType.String,
                searchable=True,
            ),
            SearchField(
                name="vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=vector_size,
                vector_search_profile_name="vector-profile",
            ),
            # Add a generic payload field to store the entire data point
            SimpleField(
                name="payload",
                type=SearchFieldDataType.String,
                searchable=False,
            ),
        ]

        # Configure vector search
        vector_search = VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="hnsw-algorithm",
                    parameters={
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name="vector-profile",
                    algorithm_configuration_name="hnsw-algorithm",
                )
            ],
        )

        # Create the search index
        index = SearchIndex(
            name=sanitized_name,
            fields=fields,
            vector_search=vector_search,
        )

        self.index_client.create_or_update_index(index)

    async def create_data_points(self, collection_name: str, data_points: List[DataPoint]):
        """Upload data points to the search index."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            await self.create_collection(collection_name)

        # Embed the data
        data_vectors = await self.embed_data(
            [DataPoint.get_embeddable_data(data_point) for data_point in data_points]
        )

        # Prepare documents for upload
        documents = []
        for i, data_point in enumerate(data_points):
            properties = get_own_properties(data_point)

            # Convert payload to proper JSON string
            import json

            document = {
                "id": str(data_point.id),
                "text": DataPoint.get_embeddable_data(data_point),
                "vector": data_vectors[i],
                "payload": json.dumps(properties),  # Store as proper JSON string
            }
            documents.append(document)

        # Upload documents
        async with self.get_async_search_client(sanitized_name) as client:
            result = await client.upload_documents(documents=documents)

            # Check for any failures
            failed_docs = [doc for doc in result if not doc.succeeded]
            if failed_docs:
                logger.error(f"Failed to upload {len(failed_docs)} documents to Azure AI Search")
                for doc in failed_docs:
                    logger.error(f"Document {doc.key} failed: {doc.error_message}")

    async def retrieve(self, collection_name: str, data_point_ids: List[str]) -> List[ScoredResult]:
        """Retrieve documents by their IDs."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        async with self.get_async_search_client(sanitized_name) as client:
            results = []

            # Azure AI Search requires individual document lookups
            for doc_id in data_point_ids:
                try:
                    document = await client.get_document(key=doc_id)

                    # Parse the payload back from JSON string
                    import json
                    payload_str = document.get("payload", "{}")
                    try:
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        # Try to parse as Python literal if JSON parsing fails
                        import ast
                        try:
                            payload = ast.literal_eval(payload_str)
                        except (ValueError, SyntaxError):
                            # If both fail, use empty dict
                            payload = {}

                    results.append(
                        ScoredResult(
                            id=parse_id(document["id"]),
                            payload=payload,
                            score=0,  # No score for direct retrieval
                        )
                    )
                except ResourceNotFoundError:
                    logger.warning(f"Document with ID '{doc_id}' not found in index '{collection_name}'")
                    continue

            return results

    async def search(
            self,
            collection_name: str,
            query_text: Optional[str] = None,
            query_vector: Optional[List[float]] = None,
            limit: int = 15,
            with_vector: bool = False,
            normalized: bool = True,
    ) -> List[ScoredResult]:
        """Perform vector or hybrid search."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if query_text is None and query_vector is None:
            raise InvalidValueError(message="One of query_text or query_vector must be provided!")

        if not await self.has_collection(collection_name):
            logger.warning(f"Index '{collection_name}' not found in AzureAISearchAdapter.search; returning [].")
            return []

        if query_vector is None and query_text:
            query_vector = (await self.embed_data([query_text]))[0]

        # Ensure limit is within Azure AI Search's valid range (1-10000)
        limit = max(1, min(limit, 10000))

        async with self.get_async_search_client(sanitized_name) as client:
            # Create vector query
            vector_query = VectorizedQuery(
                vector=query_vector,
                k_nearest_neighbors=limit,
                fields="vector",
            )

            # Perform search
            if query_text:
                # Hybrid search (text + vector)
                results = await client.search(
                    search_text=query_text,
                    vector_queries=[vector_query],
                    top=limit,
                )
            else:
                # Pure vector search
                results = await client.search(
                    search_text="*",  # Match all for pure vector search
                    vector_queries=[vector_query],
                    top=limit,
                )

            scored_results = []
            async for result in results:
                import json

                # Handle both proper JSON and Python string representation
                payload_str = result.get("payload", "{}")
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    # Try to parse as Python literal if JSON parsing fails
                    import ast
                    try:
                        payload = ast.literal_eval(payload_str)
                    except (ValueError, SyntaxError):
                        # If both fail, use empty dict
                        payload = {}

                scored_results.append(
                    ScoredResult(
                        id=parse_id(result["id"]),
                        payload={
                            **payload,
                            "id": parse_id(result["id"]),
                        },
                        score=result["@search.score"] if normalized else 1 - result["@search.score"],
                    )
                )

            return scored_results

    async def batch_search(
            self,
            collection_name: str,
            query_texts: List[str],
            limit: int = None,
            with_vectors: bool = False,
    ) -> List[List[ScoredResult]]:
        """Perform batch vector search."""
        query_vectors = await self.embed_data(query_texts)

        # Use default limit if not provided
        if limit is None:
            limit = 15

        # Azure AI Search doesn't have native batch search, so we parallelize individual searches
        results = await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=limit,
                    with_vector=with_vectors,
                )
                for query_vector in query_vectors
            ]
        )

        return results

    async def delete_data_points(self, collection_name: str, data_point_ids: List[str]):
        """Delete documents by their IDs."""
        sanitized_name = self._sanitize_index_name(collection_name)

        if not await self.has_collection(collection_name):
            raise CollectionNotFoundError(f"Index '{collection_name}' not found!")

        async with self.get_async_search_client(sanitized_name) as client:
            # Prepare documents for deletion (only need the id field)
            documents = [{"id": doc_id} for doc_id in data_point_ids]

            result = await client.delete_documents(documents=documents)

            # Check for any failures
            failed_docs = [doc for doc in result if not doc.succeeded]
            if failed_docs:
                logger.error(f"Failed to delete {len(failed_docs)} documents from Azure AI Search")
                for doc in failed_docs:
                    logger.error(f"Document {doc.key} failed: {doc.error_message}")

    async def create_vector_index(self, index_name: str, index_property_name: str):
        """Create a vector index for a specific property."""
        await self.create_collection(f"{index_name}_{index_property_name}")

    async def index_data_points(
            self, index_name: str, index_property_name: str, data_points: List[DataPoint]
    ):
        """Index data points for a specific property."""
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            [
                IndexSchema(
                    id=str(data_point.id),
                    text=getattr(data_point, data_point.metadata["index_fields"][0]),
                )
                for data_point in data_points
            ],
        )

    async def prune(self):
        """Delete all indexes."""
        try:
            # List all indexes
            indexes = self.index_client.list_indexes()

            for index in indexes:
                try:
                    self.index_client.delete_index(index.name)
                    logger.info(f"Deleted index: {index.name}")
                except Exception as error:
                    logger.error(f"Error deleting index {index.name}: {str(error)}")

        except Exception as error:
            logger.error(f"Error during prune operation: {str(error)}")
            raise error