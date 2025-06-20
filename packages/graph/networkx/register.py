from cognee.infrastructure.databases.graph import use_graph_adapter

from .networkx_adapter import NetworkXAdapter

use_graph_adapter("networkx", NetworkXAdapter)