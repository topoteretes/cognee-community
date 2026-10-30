"""Offline cognee conformance tests. No TypeDB server, no secrets."""

import pytest
from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered

from cognee_community_graph_adapter_typedb import TypeDBAdapter, register


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(TypeDBAdapter)


def test_register_adds_typedb_provider():
    register()
    assert_registered("typedb", TypeDBAdapter)


def test_constructor_maps_cognee_config_to_typedb_address():
    adapter = TypeDBAdapter(
        graph_database_url="typedb://typedb.example.com",
        graph_database_username="user",
        graph_database_password="secret",
        graph_database_port=1730,
        graph_database_key="",
        database_name="my_db",
    )
    assert adapter.address == "typedb.example.com:1730"
    assert adapter.username == "user"
    assert adapter.password == "secret"
    assert adapter.database_name == "my_db"


def test_constructor_defaults():
    adapter = TypeDBAdapter()
    assert adapter.address == "127.0.0.1:1729"
    assert adapter.username == "admin"
    assert adapter.database_name == "cognee"


def test_register_module_import_does_not_shadow_the_function():
    import importlib

    # `import pkg.register as m` would bind the re-exported *function* (the
    # package attribute), so fetch the module object itself.
    register_module = importlib.import_module("cognee_community_graph_adapter_typedb.register")
    importlib.reload(register_module)  # re-runs the import-time registration
    assert_registered("typedb", TypeDBAdapter)
    from cognee_community_graph_adapter_typedb import register as exported

    assert callable(exported)


def test_register_adds_typedb_dataset_database_handler():
    from cognee.infrastructure.databases.dataset_database_handler import (
        supported_dataset_database_handlers,
    )

    from cognee_community_graph_adapter_typedb import TypeDBDatasetDatabaseHandler

    register()
    entry = supported_dataset_database_handlers["typedb"]
    assert entry["handler_instance"] is TypeDBDatasetDatabaseHandler
    assert entry["handler_provider"] == "typedb"


def test_dataset_database_name_derivation_and_validation():
    import uuid

    import pytest

    from cognee_community_graph_adapter_typedb import TypeDBDatasetDatabaseHandler

    dataset_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert (
        TypeDBDatasetDatabaseHandler._database_name_for_dataset(dataset_id)
        == "cognee_12345678123456781234567812345678"
    )
    with pytest.raises(ValueError):
        TypeDBDatasetDatabaseHandler._database_name_for_dataset(None)
    for foreign in (
        "cognee",
        "typedb",
        "cognee_../x",
        "cognee_" + "a" * 70,
        "",
        "cognee_e2e",
        "cognee_test_abc",
        "cognee_" + "A" * 32,
    ):
        with pytest.raises(ValueError):
            TypeDBDatasetDatabaseHandler._validate_database_name(foreign)


def test_edge_key_is_unambiguous_for_ids_containing_separators():
    from cognee_community_graph_adapter_typedb.queries import _edge_key

    assert _edge_key("a|b", "c", "r") != _edge_key("a", "b|c", "r")
    assert _edge_key("a", "b", "r") == _edge_key("a", "b", "r")


def test_created_at_mirror_rejects_bool_and_non_int_payload_values():
    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    for value in (True, "2026-01-01", None, 1.5):
        created = TypeDBAdapter._node_upsert_row_from_properties("n", {"created_at": value}, "T")[
            "created"
        ]
        assert isinstance(created, int) and not isinstance(created, bool)
    assert (
        TypeDBAdapter._node_upsert_row_from_properties("n", {"created_at": 42}, "T")["created"]
        == 42
    )


def test_cypher_and_temporal_search_types_are_gated():
    """Cognee routes Cypher / temporal searches by these; both must refuse cleanly."""
    import asyncio

    from cognee.modules.retrieval.exceptions import SearchTypeNotSupported

    assert TypeDBAdapter.supports_cypher_queries is False
    adapter = TypeDBAdapter()
    with pytest.raises(SearchTypeNotSupported):
        asyncio.run(adapter.collect_time_ids(time_from=1, time_to=2))
    with pytest.raises(SearchTypeNotSupported):
        asyncio.run(adapter.collect_events(ids=["x"]))


async def test_provenance_fold_validates_source_ref_key_offline():
    """A malformed source_ref_key is rejected before any server contact."""
    from uuid import uuid4

    from cognee.infrastructure.databases.provenance import (
        make_source_ref_key,
        make_source_run_ref,
    )
    from cognee.infrastructure.databases.provenance.source_ref_state import (
        provenance_after_attach,
    )

    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    assert TypeDBAdapter._attach_for_batch(None, "run") is None
    with pytest.raises(ValueError):
        TypeDBAdapter._attach_for_batch("ds:test", str(uuid4()))
    with pytest.raises(ValueError):
        TypeDBAdapter._attach_for_batch(make_source_ref_key(uuid4(), uuid4()), "run-1")

    key, run = make_source_ref_key(uuid4(), uuid4()), str(uuid4())
    attach = TypeDBAdapter._attach_for_batch(key, run)
    assert attach.transition([], []) == provenance_after_attach([], [], [key], run)
    assert attach.keys == [key]
    assert attach.run_refs == [make_source_run_ref(run, key)]


def test_decode_provenance_orders_links_by_position():
    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    keys, refs = TypeDBAdapter._decode_provenance(
        {"keys": [{"k": "a", "p": 5}, {"k": "b", "p": 2}], "runs": [{"k": "r", "p": 0}]}
    )
    assert (keys, refs) == (["b", "a"], ["r"])
    assert TypeDBAdapter._decode_provenance({}) == ([], [])


def test_tls_config_from_environment():
    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    assert not TypeDBAdapter._tls_config_from_env({}).is_enabled
    assert not TypeDBAdapter._tls_config_from_env({"TYPEDB_TLS": "false"}).is_enabled
    native = TypeDBAdapter._tls_config_from_env({"TYPEDB_TLS": "true"})
    assert native.is_enabled and native.root_ca_path is None


def test_tls_root_ca_must_exist(tmp_path):
    from typedb.driver import TypeDBDriverExceptionNative

    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    with pytest.raises(TypeDBDriverExceptionNative, match="No such file"):
        TypeDBAdapter._tls_config_from_env(
            {"TYPEDB_TLS": "1", "TYPEDB_TLS_ROOT_CA": str(tmp_path / "x.pem")}
        )


def test_tls_flag_rejects_non_boolean_values():
    from cognee_community_graph_adapter_typedb.typedb_adapter import TypeDBAdapter

    for value in ("enabled", "ture", "t"):
        with pytest.raises(ValueError, match="TYPEDB_TLS"):
            TypeDBAdapter._tls_config_from_env({"TYPEDB_TLS": value})


def test_handler_rejects_half_configured_credentials():
    from types import SimpleNamespace

    from cognee.infrastructure.databases.exceptions import DatabaseCredentialsError

    from cognee_community_graph_adapter_typedb import TypeDBDatasetDatabaseHandler

    handler = TypeDBDatasetDatabaseHandler

    def config(**overrides):
        fields = {
            "graph_database_url": "127.0.0.1:1729",
            "graph_database_username": "",
            "graph_database_password": "",
        }
        return SimpleNamespace(**{**fields, **overrides})

    assert handler._connection_settings(config()) == ("127.0.0.1:1729", None, None)
    assert handler._connection_settings(config(graph_database_url="")) == (None, None, None)
    assert handler._connection_settings(
        config(graph_database_username="u", graph_database_password="p")
    ) == (
        "127.0.0.1:1729",
        "u",
        "p",
    )
    with pytest.raises(DatabaseCredentialsError):
        handler._connection_settings(config(graph_database_username="u"))
    with pytest.raises(DatabaseCredentialsError):
        handler._connection_settings(config(graph_database_password="p"))


def test_write_knobs_from_environment(monkeypatch):
    from cognee_community_graph_adapter_typedb.typedb_adapter import (
        WRITE_CHUNK_ROWS,
        WRITE_CONCURRENCY,
        TypeDBAdapter,
        _get_env_variable_as_positive_int,
    )

    for name in ("TYPEDB_WRITE_CHUNK_ROWS", "TYPEDB_WRITE_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    adapter = TypeDBAdapter()
    assert (adapter._chunk_rows, adapter._write_concurrency) == (
        WRITE_CHUNK_ROWS,
        WRITE_CONCURRENCY,
    )

    monkeypatch.setenv("TYPEDB_WRITE_CHUNK_ROWS", "500")
    monkeypatch.setenv("TYPEDB_WRITE_CONCURRENCY", "2")
    adapter = TypeDBAdapter()
    assert (adapter._chunk_rows, adapter._write_concurrency) == (500, 2)
    assert adapter._get_executor()._max_workers == 3
    adapter._close_sync()

    for bad in ("0", "-1", "many"):
        with pytest.raises(ValueError, match="TYPEDB_WRITE_CHUNK_ROWS"):
            _get_env_variable_as_positive_int(
                "TYPEDB_WRITE_CHUNK_ROWS", 200, {"TYPEDB_WRITE_CHUNK_ROWS": bad}
            )
