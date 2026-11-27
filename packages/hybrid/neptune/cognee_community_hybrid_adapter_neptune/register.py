from cognee.infrastructure.databases.graph import use_graph_adapter
from cognee.infrastructure.databases.vector import use_vector_adapter

from .neptune_analytics_adapter import NeptuneAnalyticsAdapter
from .neptune_graph_adapter import NeptuneGraphDB

# Graph only: GRAPH_DATABASE_PROVIDER=neptune
use_graph_adapter("neptune", NeptuneGraphDB)

# Graph and vector on one Neptune Analytics graph:
# GRAPH_DATABASE_PROVIDER=neptune_analytics and VECTOR_DB_PROVIDER=neptune_analytics
use_graph_adapter("neptune_analytics", NeptuneAnalyticsAdapter)
use_vector_adapter("neptune_analytics", NeptuneAnalyticsAdapter)
