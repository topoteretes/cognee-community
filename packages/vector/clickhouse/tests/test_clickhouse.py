import os
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import cognee
import pytest
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.modules.search.types import SearchType
from cognee_community_vector_adapter_clickhouse import register  # noqa: F401
from cognee_community_vector_adapter_clickhouse.ClickHouseDatasetDatabaseHandler import (
    ClickHouseDatasetDatabaseHandler,
)
from cognee_community_vector_adapter_clickhouse.clickhouse_adapter import (
    ClickHouseAdapter,
    ClickHouseDataPoint,
    serialize_for_json,
)


class DeterministicEmbeddingEngine:
    def get_vector_size(self) -> int:
        return 3

    async def embed_text(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        text = text.lower()
        if "hybrid" in text:
            return [0.8, 0.2, 0.0]
        if "quantum" in text:
            return [0.0, 1.0, 0.0]
        if "nlp" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 0.0, 1.0]


CLICKHOUSE_ENV_NAMES = [
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USERNAME",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_SECURE",
    "CLICKHOUSE_COMPRESS",
    "CLICKHOUSE_VERIFY",
    "CLICKHOUSE_CA_CERT",
    "CLICKHOUSE_CLIENT_CERT",
    "CLICKHOUSE_CLIENT_CERT_KEY",
    "CLICKHOUSE_CONNECT_TIMEOUT",
    "CLICKHOUSE_SEND_RECEIVE_TIMEOUT",
    "CLICKHOUSE_URL",
    "CLICKHOUSE_KEY",
    "VECTOR_DB_URL",
    "VECTOR_DB_KEY",
    "VECTOR_DB_HOST",
    "VECTOR_DB_PORT",
    "VECTOR_DB_NAME",
    "VECTOR_DB_USERNAME",
    "VECTOR_DB_PASSWORD",
    "VECTOR_DB_SECURE",
    "COGNEE_CLICKHOUSE_TABLE_PREFIX",
    "COGNEE_CLICKHOUSE_NORMALIZE_VECTORS",
    "COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX",
]


def clear_clickhouse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLICKHOUSE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def clickhouse_url() -> str:
    return (
        os.getenv("CLICKHOUSE_URL")
        or os.getenv("VECTOR_DB_URL")
        or "http://127.0.0.1:8123/cognee"
    )


def clickhouse_key() -> str:
    return (
        os.getenv("CLICKHOUSE_KEY")
        or os.getenv("VECTOR_DB_KEY")
        or os.getenv("CLICKHOUSE_PASSWORD")
        or ""
    )


def clickhouse_available() -> bool:
    try:
        adapter = ClickHouseAdapter(
            url=clickhouse_url(),
            api_key=clickhouse_key(),
            database_name="availability_check",
            embedding_engine=DeterministicEmbeddingEngine(),
        )
        result = adapter._fetch_one_sync("SELECT 1 AS ok")
        return bool(result and int(result["ok"]) == 1)
    except Exception:
        return False


@pytest.fixture(scope="session")
def require_clickhouse():
    if not clickhouse_available():
        pytest.skip("ClickHouse is not reachable; skipping live ClickHouse tests.")


def test_connection_config_from_url_and_env(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    monkeypatch.setenv("CLICKHOUSE_HOST", "env-host")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8443")
    monkeypatch.setenv("CLICKHOUSE_USERNAME", "env-user")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "env-password")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "env-db")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "true")

    adapter = ClickHouseAdapter(
        url="http://url-user:url-password@url-host:8123/url-db",
        api_key="raw-password",
        embedding_engine=DeterministicEmbeddingEngine(),
    )

    assert adapter.connection_config["host"] == "env-host"
    assert adapter.connection_config["port"] == 8443
    assert adapter.connection_config["username"] == "env-user"
    assert adapter.connection_config["password"] == "env-password"
    assert adapter.connection_config["database"] == "env-db"
    assert adapter.connection_config["secure"] is True


def test_connection_config_from_json_key(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(
        url="https://url-user:url-password@clickhouse.example.com:8443/url-db",
        api_key='{"username":"key-user","password":"key-password","database":"key-db"}',
        embedding_engine=DeterministicEmbeddingEngine(),
    )

    assert adapter.connection_config["host"] == "clickhouse.example.com"
    assert adapter.connection_config["port"] == 8443
    assert adapter.connection_config["username"] == "url-user"
    assert adapter.connection_config["password"] == "key-password"
    assert adapter.connection_config["database"] == "key-db"
    assert adapter.connection_config["secure"] is True


def test_table_name_sanitization_and_identifier_quoting(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(
        embedding_engine=DeterministicEmbeddingEngine(),
    )

    table_name = adapter._table_name("Entity Name / With Symbols And A Very Long Suffix" * 3)

    assert table_name.startswith("cognee_vec_")
    assert len(table_name) <= 64
    assert table_name == table_name.lower()
    assert "`" not in table_name
    assert adapter._quote_identifier("a`b") == "`a``b`"


def test_json_serialization_and_payload_id(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    data_id = uuid4()
    serialized = serialize_for_json({"id": data_id, "values": [data_id]})
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())

    assert serialized == {"id": str(data_id), "values": [str(data_id)]}
    payload = adapter._payload_with_id('{"text":"hello"}', str(data_id))
    assert payload["text"] == "hello"
    assert payload["id"] == data_id


def test_vector_validation_and_normalization(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())

    assert adapter._validate_vector([1, 2, 3], expected_size=3) == [1.0, 2.0, 3.0]
    assert adapter._normalize_vector([3.0, 4.0, 0.0]) == [0.6, 0.8, 0.0]

    with pytest.raises(ValueError, match="must not be empty"):
        adapter._validate_vector([], expected_size=3)
    with pytest.raises(ValueError, match="dimension mismatch"):
        adapter._validate_vector([1.0, 2.0], expected_size=3)
    with pytest.raises(ValueError, match="finite"):
        adapter._validate_vector([1.0, float("nan"), 3.0], expected_size=3)


def test_filter_builder(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())

    sql, params = adapter._build_filter(["NLP", "Quantum"], "AND")
    assert "hasAll" in sql
    assert params == {"node_names": ["NLP", "Quantum"]}

    sql, params = adapter._build_filter(["NLP"], "OR")
    assert "hasAny" in sql
    assert params == {"node_names": ["NLP"]}

    sql, params = adapter._build_filter(None, "OR")
    assert sql == ""
    assert params == {}


@pytest.mark.asyncio
async def test_search_query_uses_typed_vector_parameter_and_filters(
    monkeypatch: pytest.MonkeyPatch,
):
    clear_clickhouse_env(monkeypatch)
    data_id = uuid4()
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())
    adapter.has_collection = AsyncMock(return_value=True)
    adapter._fetch_all = AsyncMock(
        return_value=[{"id": str(data_id), "payload": '{"text":"NLP"}', "score": 0.9}]
    )

    results = await adapter.search(
        "Entity_name",
        query_vector=[1.0, 0.0, 0.0],
        include_payload=True,
        node_name=["NLP", "Quantum"],
        node_name_filter_operator="AND",
        limit=5,
    )

    sql = adapter._fetch_all.call_args.args[0]
    params = adapter._fetch_all.call_args.args[1]
    assert "FINAL" in sql
    assert "dotProduct" in sql
    assert "{query_vector:Array(Float32)}" in sql
    assert "hasAll" in sql
    assert params["query_vector"] == [1.0, 0.0, 0.0]
    assert params["node_names"] == ["NLP", "Quantum"]
    assert results[0].id == data_id
    assert results[0].payload["text"] == "NLP"


@pytest.mark.asyncio
async def test_search_missing_query_parameters(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())

    with pytest.raises(MissingQueryParameterError):
        await adapter.search("Entity_name")


@pytest.mark.asyncio
async def test_unsupported_ann_falls_back_to_exact(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    monkeypatch.setenv("COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX", "true")
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())
    adapter._fetch_one = AsyncMock(return_value={"version": "25.7.9"})
    adapter._execute = AsyncMock()

    await adapter._ensure_vector_index("cognee_vec_entities", 3)

    adapter._execute.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_lifecycle_and_filters(require_clickhouse):
    collection_name = f"adapter_collection_{uuid4().hex}"
    dataset_a = f"dataset_a_{uuid4().hex}"
    dataset_b = f"dataset_b_{uuid4().hex}"
    embedding_engine = DeterministicEmbeddingEngine()

    adapter_a = ClickHouseAdapter(
        url=clickhouse_url(),
        api_key=clickhouse_key(),
        database_name=dataset_a,
        embedding_engine=embedding_engine,
    )
    adapter_b = ClickHouseAdapter(
        url=clickhouse_url(),
        api_key=clickhouse_key(),
        database_name=dataset_b,
        embedding_engine=embedding_engine,
    )

    await adapter_a.prune()
    await adapter_b.prune()

    nlp_id = uuid4()
    hybrid_id = uuid4()
    quantum_id = uuid4()
    other_dataset_id = uuid4()

    await adapter_a.create_data_points(
        collection_name,
        [
            ClickHouseDataPoint(id=nlp_id, text="NLP document", belongs_to_set=["NLP"]),
            ClickHouseDataPoint(
                id=hybrid_id,
                text="Hybrid NLP quantum note",
                belongs_to_set=["NLP", "Quantum"],
            ),
            ClickHouseDataPoint(
                id=quantum_id,
                text="Quantum computers",
                belongs_to_set=["Quantum", "Computers"],
            ),
        ],
    )
    await adapter_b.create_data_points(
        collection_name,
        [
            ClickHouseDataPoint(
                id=other_dataset_id,
                text="NLP document from another dataset",
                belongs_to_set=["NLP"],
            )
        ],
    )

    assert await adapter_a.has_collection(collection_name)

    retrieved = await adapter_a.retrieve(collection_name, [str(nlp_id), str(hybrid_id)])
    assert {payload["id"] for payload in retrieved} == {nlp_id, hybrid_id}

    search_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=2,
    )
    assert search_results[0].payload["id"] == nlp_id

    or_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
        node_name=["NLP", "Quantum"],
        node_name_filter_operator="OR",
    )
    assert {result.payload["id"] for result in or_results} == {nlp_id, hybrid_id, quantum_id}

    and_results = await adapter_a.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
        node_name=["NLP", "Quantum"],
        node_name_filter_operator="AND",
    )
    assert {result.payload["id"] for result in and_results} == {hybrid_id}

    batch_results = await adapter_a.batch_search(
        collection_name,
        query_texts=["NLP", "Quantum"],
        include_payload=True,
        limit=1,
    )
    assert len(batch_results) == 2
    assert all(len(result_group) == 1 for result_group in batch_results)

    tenant_b_results = await adapter_b.search(
        collection_name,
        query_text="NLP",
        include_payload=True,
        limit=None,
    )
    assert {result.payload["id"] for result in tenant_b_results} == {other_dataset_id}

    replacement_id = uuid4()
    await adapter_a.create_data_points(
        collection_name,
        [ClickHouseDataPoint(id=replacement_id, text="NLP old", belongs_to_set=["Old"])],
    )
    await adapter_a.create_data_points(
        collection_name,
        [ClickHouseDataPoint(id=replacement_id, text="NLP new", belongs_to_set=["New"])],
    )
    replacement = await adapter_a.retrieve(collection_name, [str(replacement_id)])
    assert replacement[0]["text"] == "NLP new"

    deleted = await adapter_a.delete_data_points(collection_name, [str(nlp_id)])
    assert deleted["deleted"] == 1
    assert await adapter_a.retrieve(collection_name, [str(nlp_id)]) == []

    await adapter_a.prune()
    assert await adapter_a.get_collection_names() == []
    assert len(await adapter_b.get_collection_names()) == 1

    await adapter_b.prune()
    assert await adapter_b.get_collection_names() == []


@pytest.mark.asyncio
async def test_dataset_handler_metadata_and_delete(require_clickhouse):
    from cognee import config

    dataset_id = uuid4()
    config.set_vector_db_config(
        {
            "vector_db_provider": "clickhouse",
            "vector_db_url": clickhouse_url(),
            "vector_db_key": clickhouse_key(),
        }
    )

    dataset_config = await ClickHouseDatasetDatabaseHandler.create_dataset(dataset_id, None)
    assert dataset_config["vector_database_provider"] == "clickhouse"
    assert dataset_config["vector_database_name"] == str(dataset_id)
    assert dataset_config["vector_dataset_database_handler"] == "clickhouse"

    adapter = ClickHouseAdapter(
        url=clickhouse_url(),
        api_key=clickhouse_key(),
        database_name=str(dataset_id),
        embedding_engine=DeterministicEmbeddingEngine(),
    )
    collection_name = f"dataset_delete_{uuid4().hex}"
    await adapter.create_data_points(
        collection_name,
        [ClickHouseDataPoint(id=uuid4(), text="NLP document", belongs_to_set=["NLP"])],
    )
    assert len(await adapter.get_collection_names()) == 1

    dataset_database = SimpleNamespace(
        vector_database_provider="clickhouse",
        vector_database_url=clickhouse_url(),
        vector_database_key=clickhouse_key(),
        vector_database_name=str(dataset_id),
    )

    with patch(
        "cognee_community_vector_adapter_clickhouse."
        "ClickHouseDatasetDatabaseHandler.create_vector_engine",
        return_value=adapter,
    ):
        await ClickHouseDatasetDatabaseHandler.delete_dataset(dataset_database)

    assert await adapter.get_collection_names() == []


@pytest.mark.asyncio
async def test_vector_index_smoke(require_clickhouse, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX", "true")
    adapter = ClickHouseAdapter(
        url=clickhouse_url(),
        api_key=clickhouse_key(),
        database_name=f"ann_{uuid4().hex}",
        embedding_engine=DeterministicEmbeddingEngine(),
    )
    collection_name = f"ann_collection_{uuid4().hex}"
    await adapter.create_collection(collection_name)

    version_result = await adapter._fetch_one("SELECT version() AS version")
    version = str(version_result["version"]) if version_result else "0"
    if adapter._version_at_least(version, (25, 8)):
        assert await adapter.has_vector_index(collection_name)

    await adapter.prune()


@pytest.mark.asyncio
async def test_cognee_recall_flow(require_clickhouse):
    if not os.getenv("LLM_API_KEY") or not os.getenv("EMBEDDING_API_KEY"):
        pytest.skip("Cognee recall flow requires LLM_API_KEY and EMBEDDING_API_KEY.")

    os.environ.setdefault("VECTOR_DATASET_DATABASE_HANDLER", "clickhouse")

    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "clickhouse",
            "vector_db_url": clickhouse_url(),
            "vector_db_key": clickhouse_key(),
        }
    )
    cognee.config.set_graph_db_config({"graph_database_provider": "ladybug"})

    data_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".data_storage/test_clickhouse")
        ).resolve()
    )
    cognee.config.data_root_directory(data_directory_path)
    cognee_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".cognee_system/test_clickhouse")
        ).resolve()
    )
    cognee.config.system_root_directory(cognee_directory_path)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    explanation_file_path_nlp = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Natural_language_processing.txt",
    )
    explanation_file_path_quantum = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Quantum_computers.txt",
    )

    await cognee.add([explanation_file_path_nlp], "natural_language")
    await cognee.add([explanation_file_path_quantum], "quantum")
    await cognee.cognify(["quantum", "natural_language"])

    search_results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Tell me about Quantum computers",
        datasets=["quantum"],
    )
    assert len(search_results) != 0, "The search results list is empty."

    from cognee.infrastructure.databases.vector import get_vector_engine

    vector_engine = get_vector_engine()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    assert await vector_engine.get_collection_names() == []


def test_uuid_payload_ids_remain_uuid_objects(monkeypatch: pytest.MonkeyPatch):
    clear_clickhouse_env(monkeypatch)
    adapter = ClickHouseAdapter(embedding_engine=DeterministicEmbeddingEngine())
    data_id = uuid4()
    payload = adapter._payload_with_id("{}", str(data_id))
    assert isinstance(payload["id"], UUID)
