from cognee.infrastructure.databases.vector import use_vector_adapter

from .chromadb_adapter import ChromaDBAdapter

use_vector_adapter("chromadb", ChromaDBAdapter)
