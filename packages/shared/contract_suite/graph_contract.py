"""Offline conformance checks for community graph adapters against cognee 1.4.1.

Call-shape sources (cognee v1.4.1):
- construction: cognee/infrastructure/databases/graph/get_graph_engine.py
  -> adapter(graph_database_url=..., graph_database_username=...,
             graph_database_password=..., graph_database_port=...,
             graph_database_key=..., database_name=...)
- writes: cognee/tasks/storage/add_data_points.py ALWAYS calls
  add_nodes(nodes, source_ref_key=..., pipeline_run_id=...) and
  add_edges(edges, source_ref_key=..., pipeline_run_id=...) — values may be
  None, but the keywords are always passed.
- nodeset search: cognee/modules/graph/cognee_graph/CogneeGraph.py calls
  get_nodeset_subgraph(node_type=..., node_name=...,
                       node_name_filter_operator=...).
"""

import inspect

from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface

_SELF = object()


def _bind(adapter_cls, method_name: str, *args, **kwargs):
    method = getattr(adapter_cls, method_name, None)
    assert method is not None, f"{adapter_cls.__name__} is missing {method_name}()"
    signature = inspect.signature(method)
    try:
        signature.bind(_SELF, *args, **kwargs)
    except TypeError as error:
        raise AssertionError(
            f"{adapter_cls.__name__}.{method_name}{signature} cannot be called as "
            f"cognee 1.4.1 calls it (args={args}, kwargs={kwargs}): {error}"
        ) from error


def assert_graph_contract(adapter_cls, *, check_constructor=True):
    """Assert that *adapter_cls* satisfies the cognee 1.4.1 graph adapter contract.

    Parameters:
        adapter_cls: the adapter class registered via use_graph_adapter (or the
            legacy supported_databases mutation).
        check_constructor: when True, assert the class can be constructed the
            way cognee's get_graph_engine constructs community adapters. Set
            False only for adapters constructed through a custom dataset
            database handler.
    """
    assert issubclass(adapter_cls, GraphDBInterface), (
        f"{adapter_cls.__name__} must subclass GraphDBInterface"
    )
    remaining_abstract = getattr(adapter_cls, "__abstractmethods__", frozenset())
    assert not remaining_abstract, (
        f"{adapter_cls.__name__} leaves abstract methods unimplemented and cannot "
        f"be instantiated: {sorted(remaining_abstract)}"
    )

    # The hard 1.4.1 break: add_data_points always passes these kwargs.
    _bind(adapter_cls, "add_nodes", [], source_ref_key=None, pipeline_run_id=None)
    _bind(adapter_cls, "add_edges", [], source_ref_key=None, pipeline_run_id=None)
    _bind(adapter_cls, "add_nodes", [], source_ref_key="dataset:data", pipeline_run_id="run-1")
    _bind(adapter_cls, "add_edges", [], source_ref_key="dataset:data", pipeline_run_id="run-1")

    # NodeSet-filtered retrieval shape.
    _bind(
        adapter_cls,
        "get_nodeset_subgraph",
        node_type=object,
        node_name=["some_node_set"],
        node_name_filter_operator="OR",
    )

    # Other core call shapes.
    _bind(adapter_cls, "query", "MATCH (n) RETURN n", {})
    _bind(adapter_cls, "get_graph_data")
    _bind(adapter_cls, "get_graph_metrics", include_optional=True)
    _bind(adapter_cls, "is_empty")
    _bind(adapter_cls, "delete_graph")

    if check_constructor:
        init_signature = inspect.signature(adapter_cls.__init__)
        try:
            init_signature.bind(
                _SELF,
                graph_database_url="bolt://localhost:1",
                graph_database_username="user",
                graph_database_password="password",
                graph_database_port=7687,
                graph_database_key="",
                database_name="contract_db",
            )
        except TypeError as error:
            raise AssertionError(
                f"{adapter_cls.__name__}.__init__{init_signature} cannot be constructed "
                f"the way cognee's get_graph_engine constructs community adapters: {error}"
            ) from error


def assert_registered(provider_key: str, adapter_cls):
    """Assert the adapter is registered under *provider_key* after import."""
    from cognee.infrastructure.databases.graph.supported_databases import (
        supported_databases,
    )

    assert supported_databases.get(provider_key) is adapter_cls, (
        f"expected supported_databases[{provider_key!r}] to be {adapter_cls.__name__}, "
        f"got {supported_databases.get(provider_key)!r}"
    )
