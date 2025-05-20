from cognee.infrastructure.databases.vector import use_vector_adapter

from .qdrant_adapter import QDrantAdapter

use_vector_adapter("qdrant", QDrantAdapter)
