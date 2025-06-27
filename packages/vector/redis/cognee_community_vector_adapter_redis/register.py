from cognee.infrastructure.databases.vector import use_vector_adapter

from .redis_adapter import RedisAdapter

use_vector_adapter("redis", RedisAdapter)
