from cognee.infrastructure.databases.dataset_database_handler import use_dataset_database_handler
from cognee.infrastructure.databases.vector import use_vector_adapter

from .topk_adapter import TopKAdapter
from .TopKDatasetDatabaseHandler import TopKDatasetDatabaseHandler

use_vector_adapter("topk", TopKAdapter)
use_dataset_database_handler("topk", TopKDatasetDatabaseHandler, "topk")
