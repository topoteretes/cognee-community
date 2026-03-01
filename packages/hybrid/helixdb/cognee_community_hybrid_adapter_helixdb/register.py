from cognee.infrastructure.databases.dataset_database_handler import use_dataset_database_handler
from cognee.infrastructure.databases.graph import use_graph_adapter
from cognee.infrastructure.databases.vector import use_vector_adapter

from .helixdb_adapter import HelixDBAdapter
from .HelixDatasetDatabaseHandlerGraphLocal import HelixDatasetDatabaseHandlerGraphLocal
from .HelixDatasetDatabaseHandlerVectorLocal import HelixDatasetDatabaseHandlerVectorLocal

use_vector_adapter("helixdb", HelixDBAdapter)
use_graph_adapter("helixdb", HelixDBAdapter)
use_dataset_database_handler(
    "helixdb_graph_local", HelixDatasetDatabaseHandlerGraphLocal, "helixdb"
)
use_dataset_database_handler(
    "helixdb_vector_local", HelixDatasetDatabaseHandlerVectorLocal, "helixdb"
)
