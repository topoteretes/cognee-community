from cognee.infrastructure.databases.dataset_database_handler import use_dataset_database_handler
from cognee.infrastructure.databases.graph import use_graph_adapter
from cognee.infrastructure.databases.vector import use_vector_adapter

from .arcadedb_adapter import ArcadeDBAdapter
from .ArcadeDBDatasetDatabaseHandlerGraphLocal import ArcadeDBDatasetDatabaseHandlerGraphLocal
from .ArcadeDBDatasetDatabaseHandlerVectorLocal import ArcadeDBDatasetDatabaseHandlerVectorLocal

use_vector_adapter("arcadedb", ArcadeDBAdapter)
use_graph_adapter("arcadedb", ArcadeDBAdapter)
use_dataset_database_handler(
    "arcadedb_graph_local", ArcadeDBDatasetDatabaseHandlerGraphLocal, "arcadedb"
)
use_dataset_database_handler(
    "arcadedb_vector_local", ArcadeDBDatasetDatabaseHandlerVectorLocal, "arcadedb"
)
