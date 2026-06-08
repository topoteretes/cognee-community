import asyncio

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.aio import SearchClient as AsyncSearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery
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
        url: str | None = None,
        api_key: str | None = None,
        embedding_engine: EmbeddingEngine = None,
        endpoint: str | None = None,
        **kwargs,  # Accept additional keyword arguments
    ):
        # Handle both 'url' and 'endpoint' parameters
        # Also handle the 'utl' typo that appears to be in cognee
        final_endpoint = endpoint or url or kwargs.get("utl")

        if not (final_endpoint and api_key and embedding_engine):
            raise ValueError("Missing required Azure AI Search credentials!")

        self.endpoint = final_endpoint
        self.api_key = api_key
        self.embedding_engine = embedding_engine
        self.credential = AzureKeyCredential(api_key)
        self.index_client = SearchIndexClient(endpoint=self.endpoint, credential=self.credential)
        self.VECTOR_DB_LOCK = asyncio.Lock()

    def _sanitize_index_name(self, name: str) -> str:
        """
        Sanitize index name to meet Azure AI Search requirements:
        - Only lowercase letters, digits, or dashes
        - Cannot start or end with dashes
        - Limited to 128 characters
        """
        import re

        # Convert to lowercase and replace invalid characters with dashes
        sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())

        # Replace multiple consecutive dashes with a single dash
        sanitized = re.sub(r"-+", "-", sanitized)

        # Remove leading and trailing dashes
        sanitized = sanitized.strip("-")

        # Ensure it doesn't start with a number (add prefix if needed)
        if sanitized and sanitized[0].isdigit():
            sanitized = "idx-" + sanitized

        # Truncate to 128 characters if necessary
        if len(sanitized) > 128:
            sanitized = sanitized[:128].rstrip("-")

        # If empty after sanitization, use a default name
        if not sanitized:
            sanitized = "default-index"

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

    async def embed_data(self, data: list[str]) -> list[list[float]]:
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
        async with self.VECTOR_DB_LOCK:
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
                # Filterable nodeset membership, used by node_name filtering in search()
                SimpleField(
                    name="belongs_to_set",
                    type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                    filterable=True,
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

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
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

            # Normalize nodeset membership to a list of string names so the filterable
            # belongs_to_set field can back node_name filtering in search().
            raw_belongs_to_set = getattr(data_point, "belongs_to_set", None) or []
            belongs_to_set = [str(getattr(item, "name", item)) for item in raw_belongs_to_set]

            document = {
                "id": str(data_point.id),
                "text": DataPoint.get_embeddable_data(data_point),
                "vector": data_vectors[i],
                "payload": json.dumps(properties),  # Store as proper JSON string
                "belongs_to_set": belongs_to_set,
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

    async def retrieve(self, collection_name: str, data_point_ids: list[str]) -> list[ScoredResult]:
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
                    logger.warning(
                        f"Document with ID '{doc_id}' not found in index '{collection_name}'"
                    )
                    continue

            return results

    def _build_node_name_filter(self, node_name: list[str], node_name_filter_operator: str) -> str:
        """Build an OData ``$filter`` expression restricting results to data
        points whose ``belongs_to_set`` collection field contains ANY (operator
        ``OR``) or ALL (operator ``AND``) of ``node_name``.

        For OR, a single ``belongs_to_set/any(t: search.in(t, 'a,b,c'))`` clause
        matches when at least one of the names is present.  For AND, the clauses
        are chained per-name so that every requested name must be present.
        """

        def _escape(name: str) -> str:
            # Escape single quotes for OData string literals.
            return name.replace("'", "''")

        if node_name_filter_operator == "AND":
            clauses = [f"belongs_to_set/any(t: t eq '{_escape(name)}')" for name in node_name]
            return " and ".join(clauses)

        # OR (default): any of the requested names present.
        joined = ",".join(_escape(name) for name in node_name)
        return f"belongs_to_set/any(t: search.in(t, '{joined}', ','))"

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
        normalized: bool = True,
    ) -> list[ScoredResult]:
        """Perform vector or hybrid search.
        Args:
            node_name: When provided, results are filtered to data points whose
                ``belongs_to_set`` collection field contains ANY (operator
                ``OR``) or ALL (operator ``AND``) of these names.
            node_name_filter_operator: ``"OR"`` (default) or ``"AND"``,
                controlling how ``node_name`` entries are combined.
            normalized: When True (default), returns ``1 - similarity``
                so that lower scores indicate better matches, consistent
                with cognee's ``ScoredResult`` contract.  When False,
                returns the raw Azure ``@search.score`` (cosine
                similarity, higher = better).
        """
        sanitized_name = self._sanitize_index_name(collection_name)

        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if not await self.has_collection(collection_name):
            logger.warning(
                f"Index '{collection_name}' not found in AzureAISearchAdapter.search; returning []."
            )
            return []

        if query_vector is None and query_text:
            query_vector = (await self.embed_data([query_text]))[0]

        # Ensure limit is within Azure AI Search's valid range (1-10000)
        if limit and limit > 0:
            limit = min(limit, 10000)
        else:
            limit = 10000

        # Build an OData $filter over the belongs_to_set collection field.
        filter_expression = None
        if node_name:
            filter_expression = self._build_node_name_filter(node_name, node_name_filter_operator)

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
                    filter=filter_expression,
                )
            else:
                # Pure vector search
                results = await client.search(
                    search_text="*",  # Match all for pure vector search
                    vector_queries=[vector_query],
                    top=limit,
                    filter=filter_expression,
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
                        score=1 - result["@search.score"]
                        if normalized
                        else result["@search.score"],
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
                    include_payload=include_payload,
                    node_name=node_name,
                )
                for query_vector in query_vectors
            ]
        )

        return results

    async def delete_data_points(self, collection_name: str, data_point_ids: list[str]):
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
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
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
