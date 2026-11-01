from cognee.infrastructure.databases.dataset_database_handler import (
    use_dataset_database_handler,
)
from cognee.infrastructure.databases.vector import use_vector_adapter

from .InfinoDatasetDatabaseHandler import InfinoDatasetDatabaseHandler
from .infino_adapter import InfinoAdapter

use_vector_adapter("infino", InfinoAdapter)
use_dataset_database_handler("infino", InfinoDatasetDatabaseHandler, "infino")
