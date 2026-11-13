from unittest.mock import MagicMock, patch

import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_snowflake.snowflake import (
    SnowflakeClient,
    snowflake_source,
)


def test_auth_missing_credentials_raises_error():
    """Verify that omitting account or user raises ValueError."""
    with pytest.raises(ValueError, match="Snowflake authentication requires account and user"):
        snowflake_source(account=None, user=None)


def test_auth_missing_private_key_raises_error():
    """Verify that omitting private key configuration raises ValueError."""
    with pytest.raises(ValueError, match="Snowflake Key-Pair authentication requires"):
        snowflake_source(account="acct123", user="test_user")


def test_client_rsa_key_parsing(mock_rsa_pem):
    """Verify RSA private key PEM is parsed into PKCS#8 DER bytes."""
    client = SnowflakeClient(
        account="acct123",
        user="test_user",
        private_key_pem=mock_rsa_pem,
    )
    der_bytes = client._get_private_key_der()
    assert isinstance(der_bytes, bytes)
    assert len(der_bytes) > 0


def test_comments_resource_document_mode_and_formatting(mock_snowflake_client):
    """Verify schema and column comments are rendered as document cards."""
    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        include_comments=True,
        client=mock_snowflake_client,
    )

    resources = list(source.selected_resources.values())
    comments_resource = next(r for r in resources if r.name == "snowflake_comments")

    # Document mode verification
    assert getattr(comments_resource, DOCUMENT_SOURCE_ATTR) == "snowflake"

    cards = list(comments_resource())
    assert len(cards) == 1
    card = cards[0]
    assert card["id"] == "snowflake:test_acct:test_db.public.customers:schema_card"
    assert "Table: TEST_DB.PUBLIC.CUSTOMERS" in card["title"]
    assert "# Table: TEST_DB.PUBLIC.CUSTOMERS" in card["content"]
    assert "Application verified customers" in card["content"]
    assert "- `CUSTOMER_ID` (NUMBER): Primary customer key" in card["content"]
    assert "- `EMAIL` (VARCHAR): Contact email" in card["content"]


def test_tables_changes_feed_upserts_and_deletes(mock_snowflake_client):
    """Verify CHANGES clause processes inserts as upserts and deletes as tombstones."""
    tables = [
        {
            "database": "TEST_DB",
            "schema": "PUBLIC",
            "table": "CUSTOMERS",
            "primary_key": "CUSTOMER_ID",
            "use_changes": True,
        }
    ]

    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        tables=tables,
        include_comments=False,
        client=mock_snowflake_client,
    )

    tables_resource = source.selected_resources["snowflake_tables"]

    # Seed resource state with prior checkpoint
    state = {
        "tables": {
            "test_db.public.customers": {
                "last_checkpoint": "2026-10-09 00:00:00",
                "known_ids": ["101", "102"],
            }
        }
    }
    with patch("dlt.current.resource_state", return_value=state):
        rows = list(tables_resource())

    # Row 103 is an insert
    inserted = next(r for r in rows if r["id"].endswith(":103"))
    assert inserted["_deleted"] is False
    assert inserted["EMAIL"] == "new@example.com"
    assert "METADATA$ACTION" not in inserted

    # Row 102 is a delete tombstone
    deleted = next(r for r in rows if r["id"].endswith(":102"))
    assert deleted["_deleted"] is True


def test_tables_changes_retention_expiry_fallback(mock_snowflake_client):
    """Verify that a CHANGES query failure triggers graceful fallback reconciliation."""
    mock_snowflake_client.fetch_changes = MagicMock(
        side_effect=Exception("Time travel retention period has expired for table CUSTOMERS")
    )

    tables = [
        {
            "database": "TEST_DB",
            "schema": "PUBLIC",
            "table": "CUSTOMERS",
            "primary_key": "CUSTOMER_ID",
            "use_changes": True,
        }
    ]

    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        tables=tables,
        include_comments=False,
        client=mock_snowflake_client,
    )

    tables_resource = source.selected_resources["snowflake_tables"]

    state = {
        "tables": {
            "test_db.public.customers": {
                "last_checkpoint": "2026-10-01 00:00:00",
                "known_ids": ["101", "102", "999"],
            }
        }
    }

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(tables_resource())

    # Full reconcile yields 101 and 102, and tombstones missing 999
    assert any(r["id"].endswith(":101") and r["_deleted"] is False for r in rows)
    assert any(r["id"].endswith(":102") and r["_deleted"] is False for r in rows)
    assert any(r["id"].endswith(":999") and r["_deleted"] is True for r in rows)


def test_tables_timestamp_fallback_and_ties(mock_snowflake_client):
    """Verify timestamp fallback correctly handles ties via keys_seen_at_cursor."""
    tables = [
        {
            "database": "TEST_DB",
            "schema": "PUBLIC",
            "table": "CUSTOMERS",
            "primary_key": "CUSTOMER_ID",
            "use_changes": False,
            "cursor_column": "UPDATED_AT",
        }
    ]

    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        tables=tables,
        include_comments=False,
        client=mock_snowflake_client,
    )

    tables_resource = source.selected_resources["snowflake_tables"]

    # Simulate existing cursor at 2026-10-09 10:00:00 with key 102 already seen
    state = {
        "tables": {
            "test_db.public.customers": {
                "last_cursor": "2026-10-09 10:00:00",
                "keys_seen_at_cursor": ["102"],
                "known_ids": ["101", "102"],
            }
        }
    }

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(tables_resource())

    # Only new customer 104 should be emitted; 102 is skipped due to tie tracking
    upserts = [r for r in rows if not r.get("_deleted")]
    assert len(upserts) == 1
    assert upserts[0]["id"].endswith(":104")
    assert upserts[0]["EMAIL"] == "david@example.com"


def test_deletion_safety_aborts_on_scan_failure(mock_snowflake_client):
    """Verify that a key scan failure aborts deletion reconciliation without tombstones."""
    mock_snowflake_client.fetch_table_keys = MagicMock(
        side_effect=Exception("Warehouse queue timeout or permission denied")
    )

    tables = [
        {
            "database": "TEST_DB",
            "schema": "PUBLIC",
            "table": "CUSTOMERS",
            "primary_key": "CUSTOMER_ID",
            "use_changes": False,
            "cursor_column": "UPDATED_AT",
        }
    ]

    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        tables=tables,
        include_comments=False,
        client=mock_snowflake_client,
    )

    tables_resource = source.selected_resources["snowflake_tables"]

    state = {
        "tables": {
            "test_db.public.customers": {
                "last_cursor": "2026-10-09 08:00:00",
                "known_ids": ["101", "102", "999"],
            }
        }
    }

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(tables_resource())

    # Zero deleted tombstones must be emitted when inventory scan fails
    tombstones = [r for r in rows if r.get("_deleted") is True]
    assert len(tombstones) == 0


def test_explicit_queries_execution(mock_snowflake_client):
    """Verify opt-in queries yield rows with stable query IDs."""
    queries = [
        {
            "name": "large_orders",
            "sql": "SELECT ORDER_ID, AMOUNT, CUSTOMER_ID FROM TEST_DB.PUBLIC.ORDERS",
            "primary_key": "ORDER_ID",
        }
    ]

    source = snowflake_source(
        account="test_acct",
        user="test_user",
        database="TEST_DB",
        queries=queries,
        include_comments=False,
        client=mock_snowflake_client,
    )

    queries_resource = source.selected_resources["snowflake_queries"]
    rows = list(queries_resource())

    assert len(rows) == 2
    assert rows[0]["id"] == "snowflake:test_acct:query:large_orders:901"
    assert rows[0]["AMOUNT"] == 500
    assert rows[1]["id"] == "snowflake:test_acct:query:large_orders:902"
    assert rows[1]["AMOUNT"] == 1200


def test_credential_privacy(mock_rsa_pem):
    """Verify private key and passphrases are never leaked in string representations."""
    client = SnowflakeClient(
        account="acct123",
        user="test_user",
        private_key_pem=mock_rsa_pem,
        private_key_passphrase="super_secret_passphrase",
    )
    assert "super_secret_passphrase" not in repr(client)
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in repr(client)
