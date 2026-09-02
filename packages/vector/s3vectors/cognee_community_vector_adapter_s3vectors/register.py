from cognee.infrastructure.databases.vector import use_vector_adapter

from .s3vectors_adapter import S3VectorsAdapter

use_vector_adapter("s3vectors", S3VectorsAdapter)
