from cognee.infrastructure.databases.vector import use_vector_adapter

from .azureaisearch_adapter import AzureAISearchAdapter

use_vector_adapter("azureaisearch", AzureAISearchAdapter)
