from __future__ import annotations

import pytest

from cognee_community_connector_hydradb.hydradb import (
    HydraDBAPIError,
    HydraDBAuthError,
    HydraDBClient,
    HydraDBNotFoundError,
    HydraDBRateLimitError,
    _compute_hash,
    fetch_hydradb_contexts,
    hydradb_source,
)
from tests.fake_hydradb import FakeHydraDBClient


def test_hash_computation():
    content = "Sample document content for HydraDB testing."
    h1 = _compute_hash(content)
    h2 = _compute_hash(content)
    assert h1 == h2
    assert len(h1) == 64
    assert _compute_hash("different") != h1


def test_client_init_and_headers():
    client = HydraDBClient(api_key="test_hydra_key", base_url="https://custom.hydradb.io/v2/")
    assert client.api_key == "test_hydra_key"
    assert client.base_url == "https://custom.hydradb.io/v2"

    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer test_hydra_key"
    assert headers["X-Hydra-Key"] == "test_hydra_key"
    assert headers["Accept"] == "application/json"


def test_fake_client_ping():
    good_client = FakeHydraDBClient(should_fail=False)
    assert good_client.ping() is True

    bad_client = FakeHydraDBClient(should_fail=True)
    assert bad_client.ping() is False


def test_fetch_hydradb_contexts_pagination_and_inspect():
    fake_client = FakeHydraDBClient()
    items = list(fetch_hydradb_contexts(client=fake_client))

    # Expecting 3 contexts across 2 pages
    assert len(items) == 3

    # Check ctx_001
    item0 = items[0]
    assert item0["id"] == "hydradb:core_engine:architecture:ctx_001"
    assert item0["context_id"] == "ctx_001"
    assert item0["title"] == "Architecture Overview"
    assert "Overview of autonomous agent architecture" in item0["text"]
    assert len(item0["content_hash"]) == 64

    # Check ctx_002 (which triggered inspect_context because raw content was None)
    item1 = items[1]
    assert item1["id"] == "hydradb:core_engine:api:ctx_002"
    assert item1["context_id"] == "ctx_002"
    assert "Full body text content for ctx_002" in item1["text"]

    # Check ctx_003 (from page 2)
    item2 = items[2]
    assert item2["id"] == "hydradb:ops:runbooks:ctx_003"
    assert item2["category"] == "operations"


def test_fetch_hydradb_contexts_scoped_parameters():
    fake_client = FakeHydraDBClient()
    list(
        fetch_hydradb_contexts(
            database_id="custom_db",
            collection_id="custom_coll",
            category="specs",
            client=fake_client,
        )
    )

    call = fake_client.calls[0]
    assert call["endpoint"] == "/databases/custom_db/collections/custom_coll/contexts"
    assert call["params"]["category"] == "specs"


def test_error_handling():
    auth_client = FakeHydraDBClient(should_fail=True, error_type="auth")
    with pytest.raises(HydraDBAuthError):
        auth_client.get("/contexts")

    not_found_client = FakeHydraDBClient(should_fail=True, error_type="not_found")
    with pytest.raises(HydraDBNotFoundError):
        not_found_client.get("/contexts/invalid")

    rate_limit_client = FakeHydraDBClient(should_fail=True, error_type="rate_limit")
    with pytest.raises(HydraDBRateLimitError):
        rate_limit_client.get("/contexts")

    api_err_client = FakeHydraDBClient(should_fail=True)
    with pytest.raises(HydraDBAPIError):
        api_err_client.get("/contexts")


def test_hydradb_source_factory():
    fake_client = FakeHydraDBClient()
    source = hydradb_source(api_key="test_key", client=fake_client)

    # Validate dlt source shape
    assert source.name == "hydradb"
    assert "hydradb_contexts" in source.resources
    res = source.resources["hydradb_contexts"]
    assert res.write_disposition == "merge"
    assert res._hints.get("primary_key") == "id"

    # Extract records through resource
    records = list(res)
    assert len(records) == 3
    assert records[0]["context_id"] == "ctx_001"
