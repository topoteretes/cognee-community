from cognee.infrastructure.databases.vector import use_vector_adapter

from .opensearch_adapter import OpenSearchAdapter

use_vector_adapter("opensearch", OpenSearchAdapter)
