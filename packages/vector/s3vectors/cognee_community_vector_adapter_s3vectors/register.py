from cognee.infrastructure.databases.dataset_database_handler import use_dataset_database_handler
from cognee.infrastructure.databases.vector import use_vector_adapter

from .s3vectors_adapter import S3VectorsAdapter
from .S3VectorsDatasetDatabaseHandler import S3VectorsDatasetDatabaseHandler

use_vector_adapter("s3vectors", S3VectorsAdapter)
use_dataset_database_handler("s3vectors", S3VectorsDatasetDatabaseHandler, "s3vectors")
