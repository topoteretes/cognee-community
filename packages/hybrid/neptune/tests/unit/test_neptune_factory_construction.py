"""The adapters are built by cognee's engine factories from the configured endpoint URL.

cognee core used to parse ``neptune-graph://<GRAPH_ID>`` itself before constructing
the adapters; that parsing now lives in the adapters. No AWS access is needed: the
client initialisation is patched out.
"""

from unittest.mock import MagicMock, patch

import pytest
from cognee_community_hybrid_adapter_neptune import NeptuneAnalyticsAdapter, NeptuneGraphDB
from cognee_community_hybrid_adapter_neptune.neptune_graph_adapter import resolve_graph_id

GRAPH_ID = "g-abc1234567"
ENDPOINT = f"neptune-graph://{GRAPH_ID}"
MODULE = "cognee_community_hybrid_adapter_neptune.neptune_graph_adapter"


@pytest.fixture(autouse=True)
def no_aws():
    with (
        patch(f"{MODULE}.NeptuneGraphDB._initialize_client", return_value=MagicMock()),
        patch(f"{MODULE}.LANGCHAIN_AWS_AVAILABLE", True),
    ):
        yield


def test_graph_adapter_built_the_way_get_graph_engine_builds_it():
    adapter = NeptuneGraphDB(
        graph_database_url=ENDPOINT,
        graph_database_username="",
        graph_database_password="",
        graph_database_port="",
        graph_database_key="",
        database_name="",
    )
    assert adapter.graph_id == GRAPH_ID


def test_hybrid_adapter_built_the_way_get_graph_engine_builds_it():
    adapter = NeptuneAnalyticsAdapter(
        graph_database_url=ENDPOINT,
        graph_database_username="",
        graph_database_password="",
        graph_database_port="",
        graph_database_key="",
        database_name="",
    )
    assert adapter.graph_id == GRAPH_ID
    assert adapter.embedding_engine is None


def test_hybrid_adapter_built_the_way_create_vector_engine_builds_it():
    embedding_engine = MagicMock()
    adapter = NeptuneAnalyticsAdapter(
        url=ENDPOINT,
        api_key="",
        embedding_engine=embedding_engine,
        database_name="",
        vector_db_host="",
        vector_db_port="",
        vector_db_username="",
        vector_db_password="",
    )
    assert adapter.graph_id == GRAPH_ID
    assert adapter.embedding_engine is embedding_engine


def test_graph_id_can_still_be_passed_directly():
    assert NeptuneGraphDB(GRAPH_ID).graph_id == GRAPH_ID
    assert NeptuneAnalyticsAdapter(GRAPH_ID).graph_id == GRAPH_ID


def test_missing_endpoint_is_reported():
    with pytest.raises(OSError, match="Missing Neptune endpoint"):
        NeptuneGraphDB(graph_database_url="")
    with pytest.raises(OSError, match="Missing Neptune endpoint"):
        NeptuneAnalyticsAdapter(url=None)


def test_endpoint_with_the_wrong_scheme_is_rejected():
    with pytest.raises(ValueError, match="neptune-graph://<GRAPH_ID>"):
        resolve_graph_id(None, "https://example.com/graph")


def test_providers_resolve_through_the_cognee_factories():
    import cognee_community_hybrid_adapter_neptune.register  # noqa: F401
    from cognee.infrastructure.databases.graph.supported_databases import (
        supported_databases as graph_databases,
    )
    from cognee.infrastructure.databases.vector.supported_databases import (
        supported_databases as vector_databases,
    )

    assert graph_databases["neptune"](graph_database_url=ENDPOINT).graph_id == GRAPH_ID
    assert graph_databases["neptune_analytics"](graph_database_url=ENDPOINT).graph_id == GRAPH_ID
    assert vector_databases["neptune_analytics"](url=ENDPOINT).graph_id == GRAPH_ID
