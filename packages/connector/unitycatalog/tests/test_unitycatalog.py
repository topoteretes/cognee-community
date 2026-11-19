"""100% offline unit tests for the Databricks Unity Catalog connector."""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cognee_community_connector_unitycatalog.client import (
    UnityCatalogAuthError,
    UnityCatalogClient,
    UnityCatalogNotFoundError,
)
from cognee_community_connector_unitycatalog.unitycatalog import (
    DOCUMENT_SOURCE_ATTR,
    UNITYCATALOG_CATALOGS_TABLE_NAME,
    UNITYCATALOG_MODELS_TABLE_NAME,
    UNITYCATALOG_SCHEMAS_TABLE_NAME,
    UNITYCATALOG_SOURCE_NAME,
    UNITYCATALOG_TABLES_TABLE_NAME,
    _catalog_to_document,
    _model_to_document,
    _schema_to_document,
    _table_to_document,
    unitycatalog_source,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sample_catalog() -> dict[str, Any]:
    return {
        "name": "production",
        "comment": "Primary enterprise lakehouse catalog",
        "owner": "data_engineering_team",
        "storage_root": "s3://enterprise-lakehouse-prod/data",
    }


@pytest.fixture
def sample_schema() -> dict[str, Any]:
    return {
        "name": "finance",
        "catalog_name": "production",
        "full_name": "production.finance",
        "comment": "General ledger and transaction streams",
        "owner": "finance_analytics",
    }


@pytest.fixture
def sample_table() -> dict[str, Any]:
    return {
        "name": "settled_transactions",
        "catalog_name": "production",
        "schema_name": "finance",
        "full_name": "production.finance.settled_transactions",
        "table_type": "MANAGED",
        "data_source_format": "DELTA",
        "comment": "Daily settled customer card and bank payments",
        "owner": "payments_team",
        "columns": [
            {
                "name": "tx_id",
                "type_text": "string",
                "type_name": "STRING",
                "comment": "Primary transaction UUID",
            },
            {
                "name": "amount_cents",
                "type_text": "bigint",
                "type_name": "BIGINT",
                "comment": "Transaction amount in smallest currency unit",
            },
            {
                "name": "currency",
                "type_text": "string",
                "type_name": "STRING",
            },
            {
                "name": "settled_at",
                "type_text": "timestamp",
                "type_name": "TIMESTAMP",
                "comment": "UTC settlement confirmation timestamp",
            },
        ],
    }


@pytest.fixture
def sample_model() -> dict[str, Any]:
    return {
        "name": "fraud_detector_xgb",
        "catalog_name": "production",
        "schema_name": "finance",
        "full_name": "production.finance.fraud_detector_xgb",
        "comment": "XGBoost real-time fraud probability model",
        "aliases": [
            {"alias_name": "champion", "version_num": 4},
            {"alias_name": "challenger", "version_num": 5},
        ],
        "tags": {
            "algorithm": "xgboost",
            "auc_pr": "0.942",
            "trained_by": "mlops_pipeline",
        },
    }


# ===========================================================================
# Client validation & security tests
# ===========================================================================


def test_client_missing_url(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("UNITY_CATALOG_URL", raising=False)
    monkeypatch.delenv("DATABRICKS_SERVER_URL", raising=False)
    with pytest.raises(ValueError, match="Unity Catalog URL required"):
        UnityCatalogClient(base_url=None, token="tok-123")


def test_client_invalid_url_scheme():
    with pytest.raises(ValueError, match="Invalid Unity Catalog URL"):
        UnityCatalogClient(base_url="ftp://lakehouse.internal", token="tok-123")

    with pytest.raises(ValueError, match="Invalid Unity Catalog URL"):
        UnityCatalogClient(base_url="not-a-url", token="tok-123")


def test_client_missing_token(monkeypatch):
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("UNITY_CATALOG_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Unity Catalog authentication token required"):
        UnityCatalogClient(base_url="https://adb-123.databricks.com", token=None)


def test_client_url_normalization():
    # Strips trailing slash
    c1 = UnityCatalogClient(base_url="https://adb-123.databricks.com/", token="tok")
    assert c1.base_url == "https://adb-123.databricks.com"

    # Trims accidental /api/2.1/unity-catalog suffix
    c2 = UnityCatalogClient(
        base_url="https://adb-123.databricks.com/api/2.1/unity-catalog", token="tok"
    )
    assert c2.base_url == "https://adb-123.databricks.com"


def test_client_auth_header():
    client = UnityCatalogClient(
        base_url="https://adb-123.databricks.com", token="dapi-super-secret-pat"
    )
    assert client._headers()["Authorization"] == "Bearer dapi-super-secret-pat"


def test_client_credential_redaction():
    client = UnityCatalogClient(
        base_url="https://adb-123.databricks.com",
        token="dapi-super-secret-pat",
    )
    msg = "Request to https://adb-123.databricks.com failed using token dapi-super-secret-pat"
    redacted = client.redact(msg)
    assert "dapi-super-secret-pat" not in redacted
    assert "[REDACTED]" in redacted


# ===========================================================================
# Client HTTP operations & error handling
# ===========================================================================


def test_client_auth_error_handling():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="bad-tok")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 401
    with (
        patch("httpx.Client.request", return_value=mock_resp),
        pytest.raises(UnityCatalogAuthError, match="authentication failed"),
    ):
        client.request("GET", "/api/2.1/unity-catalog/catalogs")


def test_client_not_found_error_handling():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="good-tok")
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404
    with (
        patch("httpx.Client.request", return_value=mock_resp),
        pytest.raises(UnityCatalogNotFoundError, match="resource not found"),
    ):
        client.request("GET", "/api/2.1/unity-catalog/catalogs/nonexistent")


def test_client_retry_and_recovery():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="tok")

    resp_fail = MagicMock(spec=httpx.Response)
    resp_fail.status_code = 503

    resp_ok = MagicMock(spec=httpx.Response)
    resp_ok.status_code = 200
    resp_ok.content = b'{"catalogs": [{"name": "prod"}]}'
    resp_ok.json.return_value = {"catalogs": [{"name": "prod"}]}

    with patch("httpx.Client.request", side_effect=[resp_fail, resp_ok]), patch("time.sleep"):
        res = client.get_catalogs()
        assert len(res) == 1
        assert res[0]["name"] == "prod"


def test_client_get_catalogs():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="tok")
    with patch.object(
        client, "request", return_value={"catalogs": [{"name": "c1"}, {"name": "c2"}]}
    ):
        cats = client.get_catalogs()
        assert len(cats) == 2
        assert cats[0]["name"] == "c1"


def test_client_get_schemas():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="tok")
    with patch.object(client, "request", return_value={"schemas": [{"name": "s1"}]}) as mock_req:
        res = client.get_schemas("c1")
        mock_req.assert_called_once_with(
            "GET", "/api/2.1/unity-catalog/schemas", params={"catalog_name": "c1"}
        )
        assert len(res) == 1
        assert res[0]["name"] == "s1"


def test_client_get_tables():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="tok")
    with patch.object(client, "request", return_value={"tables": [{"name": "t1"}]}) as mock_req:
        res = client.get_tables("c1", "s1")
        mock_req.assert_called_once_with(
            "GET",
            "/api/2.1/unity-catalog/tables",
            params={"catalog_name": "c1", "schema_name": "s1"},
        )
        assert len(res) == 1
        assert res[0]["name"] == "t1"


def test_client_get_models():
    client = UnityCatalogClient(base_url="https://adb-123.databricks.com", token="tok")
    with patch.object(client, "request", return_value={"registered_models": [{"name": "m1"}]}):
        mods = client.get_registered_models("c1", "s1")
        assert len(mods) == 1
        assert mods[0]["name"] == "m1"


# ===========================================================================
# Markdown document rendering tests
# ===========================================================================


def test_catalog_to_document_rendering(sample_catalog):
    doc = _catalog_to_document(sample_catalog, "https://adb-123.databricks.com")
    assert doc["id"] == "unitycatalog_cat_production"
    assert doc["title"] == "Unity Catalog: production"
    content = doc["content"]
    assert "# Unity Catalog: production" in content
    assert "**Owner**: `data_engineering_team`" in content
    assert "**Storage Root**: `s3://enterprise-lakehouse-prod/data`" in content
    assert "Primary enterprise lakehouse catalog" in content


def test_schema_to_document_rendering(sample_schema):
    doc = _schema_to_document(sample_schema, "https://adb-123.databricks.com")
    assert doc["id"] == "unitycatalog_schema_production_finance"
    assert doc["title"] == "Unity Catalog Schema: production.finance"
    content = doc["content"]
    assert "# Unity Catalog Schema: production.finance" in content
    assert "**Parent Catalog**: `production`" in content
    assert "General ledger and transaction streams" in content


def test_table_to_document_rendering(sample_table):
    doc = _table_to_document(sample_table, "https://adb-123.databricks.com")
    assert doc["id"] == "unitycatalog_tbl_production_finance_settled_transactions"
    assert doc["title"] == "Unity Catalog Table: production.finance.settled_transactions (DELTA)"
    content = doc["content"]
    assert "# Unity Catalog Table: production.finance.settled_transactions" in content
    assert "**Table Type**: `MANAGED`" in content
    assert "**Data Format**: `DELTA`" in content
    assert "## Column Schema" in content
    assert "`tx_id` (`string`) - *Primary transaction UUID*" in content
    assert "`amount_cents` (`bigint`) - *Transaction amount in smallest currency unit*" in content
    assert "`currency` (`string`)" in content
    assert "`settled_at` (`timestamp`)" in content


def test_model_to_document_rendering(sample_model):
    doc = _model_to_document(sample_model, "https://adb-123.databricks.com")
    assert doc["id"] == "unitycatalog_mdl_production_finance_fraud_detector_xgb"
    assert doc["title"] == "Unity Catalog Model: production.finance.fraud_detector_xgb"
    content = doc["content"]
    assert "# Unity Catalog Registered AI Model: production.finance.fraud_detector_xgb" in content
    assert "XGBoost real-time fraud probability model" in content
    assert "**champion**: Version 4" in content
    assert "**challenger**: Version 5" in content
    assert "**algorithm**: `xgboost`" in content
    assert "**auc_pr**: `0.942`" in content


# ===========================================================================
# dlt source and pipeline invariants
# ===========================================================================


def test_unitycatalog_source_attributes():
    mock_client = MagicMock(spec=UnityCatalogClient)
    mock_client.base_url = "https://adb-123.databricks.com"
    source = unitycatalog_source(client=mock_client)

    # Invariant 1: Document source attribute tag for Cognee
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == UNITYCATALOG_SOURCE_NAME

    # Invariant 2: Resources registered
    resources = {r.name: r for r in source.selected_resources.values()}
    assert UNITYCATALOG_CATALOGS_TABLE_NAME in resources
    assert UNITYCATALOG_SCHEMAS_TABLE_NAME in resources
    assert UNITYCATALOG_TABLES_TABLE_NAME in resources
    assert UNITYCATALOG_MODELS_TABLE_NAME in resources

    # Invariant 3: Write disposition is replace for automatic orphan cleanup
    assert resources[UNITYCATALOG_CATALOGS_TABLE_NAME].write_disposition == "replace"
    assert resources[UNITYCATALOG_SCHEMAS_TABLE_NAME].write_disposition == "replace"
    assert resources[UNITYCATALOG_TABLES_TABLE_NAME].write_disposition == "replace"
    assert resources[UNITYCATALOG_MODELS_TABLE_NAME].write_disposition == "replace"


def test_unitycatalog_source_selective_resources():
    mock_client = MagicMock(spec=UnityCatalogClient)
    mock_client.base_url = "https://adb-123.databricks.com"
    source = unitycatalog_source(
        client=mock_client,
        include_tables=False,
        include_models=False,
    )

    resources = {r.name: r for r in source.selected_resources.values()}
    assert UNITYCATALOG_CATALOGS_TABLE_NAME in resources
    assert UNITYCATALOG_SCHEMAS_TABLE_NAME in resources
    assert UNITYCATALOG_TABLES_TABLE_NAME not in resources
    assert UNITYCATALOG_MODELS_TABLE_NAME not in resources


def test_unitycatalog_source_execution(sample_catalog, sample_schema, sample_table, sample_model):
    mock_client = MagicMock(spec=UnityCatalogClient)
    mock_client.base_url = "https://adb-123.databricks.com"
    mock_client.get_catalogs.return_value = [sample_catalog]
    mock_client.get_schemas.return_value = [sample_schema]
    mock_client.get_tables.return_value = [sample_table]
    mock_client.get_registered_models.return_value = [sample_model]

    source = unitycatalog_source(client=mock_client)

    cat_rows = list(source.selected_resources[UNITYCATALOG_CATALOGS_TABLE_NAME])
    schema_rows = list(source.selected_resources[UNITYCATALOG_SCHEMAS_TABLE_NAME])
    table_rows = list(source.selected_resources[UNITYCATALOG_TABLES_TABLE_NAME])
    model_rows = list(source.selected_resources[UNITYCATALOG_MODELS_TABLE_NAME])

    assert len(cat_rows) == 1
    assert cat_rows[0]["id"] == "unitycatalog_cat_production"

    assert len(schema_rows) == 1
    assert schema_rows[0]["id"] == "unitycatalog_schema_production_finance"

    assert len(table_rows) == 1
    assert table_rows[0]["id"] == "unitycatalog_tbl_production_finance_settled_transactions"
    assert "settled_transactions" in table_rows[0]["title"]

    assert len(model_rows) == 1
    assert model_rows[0]["id"] == "unitycatalog_mdl_production_finance_fraud_detector_xgb"
    assert "fraud_detector_xgb" in model_rows[0]["title"]
