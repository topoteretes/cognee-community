from cognee.infrastructure.databases.vector import use_vector_adapter

from .lodedb_adapter import LodeDBAdapter

use_vector_adapter("lodedb", LodeDBAdapter)
