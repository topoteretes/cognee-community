"""Offline conformance checks for community vector adapters against cognee 1.4.1.

These assertions encode the exact call shapes cognee core uses when talking to
a vector adapter, so a package's unit tier can prove 1.4.1 compatibility with
no database, no network, and no API keys.

Call-shape sources (cognee v1.4.1):
- construction: cognee/infrastructure/databases/vector/create_vector_engine.py
  -> adapter(url=..., api_key=..., embedding_engine=..., database_name=...)
- search: cognee/modules/retrieval/chunks_retriever.py
  -> search(collection, query, limit=..., include_payload=True,
            node_name=..., node_name_filter_operator=...)
- batch_search: cognee/modules/retrieval/utils/node_edge_vector_search.py
  -> batch_search(collection_name=..., query_texts=..., limit=None)
- indexing: cognee/tasks/storage/index_data_points.py
  -> create_vector_index(type_name, field_name);
     index_data_points(type_name, field_name, batch);
     vector_engine.embedding_engine.get_batch_size()
"""

import inspect

from cognee.infrastructure.databases.vector.vector_db_interface import VectorDBInterface

from .fakes import FakeEmbeddingEngine

_SELF = object()

REQUIRED_METHODS = [
    "has_collection",
    "create_collection",
    "create_data_points",
    "create_vector_index",
    "index_data_points",
    "retrieve",
    "search",
    "batch_search",
    "delete_data_points",
    "prune",
    "embed_data",
]


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


def assert_vector_contract(adapter_cls, *, instantiate=True, constructor_kwargs=None):
    """Assert that *adapter_cls* satisfies the cognee 1.4.1 vector adapter contract.

    Parameters:
        adapter_cls: the adapter class registered via use_vector_adapter.
        instantiate: when True, also construct the adapter with a fake embedding
            engine and dummy connection info and assert instance attributes.
            Set False for adapters whose __init__ dials out to the service.
        constructor_kwargs: overrides for the dummy construction kwargs.
    """
    # VectorDBInterface is a typing.Protocol (not runtime_checkable), so
    # issubclass() raises; check the MRO for explicit subclassing instead.
    assert VectorDBInterface in adapter_cls.__mro__, (
        f"{adapter_cls.__name__} must genuinely subclass VectorDBInterface "
        "(a TYPE_CHECKING-only import does not count)"
    )

    for method_name in REQUIRED_METHODS:
        assert callable(getattr(adapter_cls, method_name, None)), (
            f"{adapter_cls.__name__} is missing required method {method_name}()"
        )

    # Factory construction shape (create_vector_engine.py).
    init_signature = inspect.signature(adapter_cls.__init__)
    try:
        init_signature.bind(
            _SELF,
            url="http://localhost:1",
            api_key="key",
            embedding_engine=FakeEmbeddingEngine(),
            database_name="contract_db",
        )
    except TypeError as error:
        raise AssertionError(
            f"{adapter_cls.__name__}.__init__{init_signature} cannot be constructed the "
            f"way cognee's create_vector_engine constructs community adapters: {error}"
        ) from error

    # Retriever search shape (positional collection+query, kwargs after).
    _bind(
        adapter_cls,
        "search",
        "DocumentChunk_text",
        "query text",
        limit=5,
        include_payload=True,
        node_name=["some_node_set"],
        node_name_filter_operator="OR",
    )
    # Brute-force triplet search shape (vector query, no text).
    _bind(
        adapter_cls,
        "search",
        collection_name="EntityName_text",
        query_vector=[0.0, 0.1],
        limit=10,
        node_name=["id-1", "id-2"],
    )
    # Batch search shape; limit=None must be accepted as a value.
    _bind(
        adapter_cls,
        "batch_search",
        collection_name="EntityName_text",
        query_texts=["a", "b"],
        limit=None,
    )
    # Indexing pipeline shapes.
    _bind(adapter_cls, "create_vector_index", "DocumentChunk", "text")
    _bind(adapter_cls, "index_data_points", "DocumentChunk", "text", [])
    _bind(adapter_cls, "create_collection", "DocumentChunk_text")
    _bind(adapter_cls, "create_data_points", "DocumentChunk_text", [])
    _bind(adapter_cls, "retrieve", "DocumentChunk_text", ["some-id"])
    _bind(adapter_cls, "delete_data_points", "DocumentChunk_text", ["some-id"])
    _bind(adapter_cls, "has_collection", "DocumentChunk_text")
    _bind(adapter_cls, "prune")

    if instantiate:
        kwargs = {
            "url": "http://localhost:1",
            "api_key": "key",
            "embedding_engine": FakeEmbeddingEngine(),
            "database_name": "contract_db",
        }
        if constructor_kwargs:
            kwargs.update(constructor_kwargs)
        adapter = adapter_cls(**kwargs)
        assert getattr(adapter, "embedding_engine", None) is not None, (
            f"{adapter_cls.__name__} must expose an `embedding_engine` attribute; "
            "cognee's index_data_points calls "
            "vector_engine.embedding_engine.get_batch_size()"
        )
        assert adapter.embedding_engine.get_batch_size() > 0


def assert_registered(provider_key: str, adapter_cls):
    """Assert the adapter is registered under *provider_key* after import."""
    from cognee.infrastructure.databases.vector.supported_databases import (
        supported_databases,
    )

    assert supported_databases.get(provider_key) is adapter_cls, (
        f"expected supported_databases[{provider_key!r}] to be {adapter_cls.__name__}, "
        f"got {supported_databases.get(provider_key)!r}"
    )
