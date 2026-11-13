from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def mock_rsa_pem() -> str:
    """Generate a valid in-memory RSA private key in PEM format."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture
def mock_snowflake_client():
    """Create a mock SnowflakeClient for offline testing."""

    class MockClient:
        def __init__(self):
            self.account = "test_acct.us-east-1"
            self.user = "TEST_USER"
            self.database = "TEST_DB"
            self.schema = "PUBLIC"

        def fetch_table_and_column_comments(
            self, database: str, schema: str | None = None
        ) -> list[dict[str, Any]]:
            return [
                {
                    "TABLE_CATALOG": "TEST_DB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "CUSTOMERS",
                    "TABLE_COMMENT": "Application verified customers.",
                    "COLUMN_NAME": "CUSTOMER_ID",
                    "DATA_TYPE": "NUMBER",
                    "COLUMN_COMMENT": "Primary customer key",
                    "ORDINAL_POSITION": 1,
                },
                {
                    "TABLE_CATALOG": "TEST_DB",
                    "TABLE_SCHEMA": "PUBLIC",
                    "TABLE_NAME": "CUSTOMERS",
                    "TABLE_COMMENT": "Application verified customers.",
                    "COLUMN_NAME": "EMAIL",
                    "DATA_TYPE": "VARCHAR",
                    "COLUMN_COMMENT": "Contact email",
                    "ORDINAL_POSITION": 2,
                },
            ]

        def fetch_table_keys(
            self, database: str, schema: str, table: str, primary_key: str
        ) -> list[Any]:
            return [101, 102]

        def fetch_changes(
            self,
            database: str,
            schema: str,
            table: str,
            last_sync: str,
            current_sync: str,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "CUSTOMER_ID": 103,
                    "EMAIL": "new@example.com",
                    "METADATA$ACTION": "INSERT",
                    "METADATA$ISUPDATE": False,
                    "METADATA$ROW_ID": "row_103",
                },
                {
                    "CUSTOMER_ID": 102,
                    "EMAIL": "old@example.com",
                    "METADATA$ACTION": "DELETE",
                    "METADATA$ISUPDATE": False,
                    "METADATA$ROW_ID": "row_102",
                },
            ]

        def fetch_rows_since_cursor(
            self,
            database: str,
            schema: str,
            table: str,
            cursor_column: str,
            cursor_val: str | None,
            primary_key: str,
        ) -> list[dict[str, Any]]:
            if cursor_val == "2026-10-09 10:00:00":
                # Returning next row with tied and new timestamp
                return [
                    {
                        "CUSTOMER_ID": 102,
                        "EMAIL": "bob@example.com",
                        "UPDATED_AT": "2026-10-09 10:00:00",
                    },
                    {
                        "CUSTOMER_ID": 104,
                        "EMAIL": "david@example.com",
                        "UPDATED_AT": "2026-10-09 11:00:00",
                    },
                ]
            return [
                {
                    "CUSTOMER_ID": 101,
                    "EMAIL": "alice@example.com",
                    "UPDATED_AT": "2026-10-09 09:00:00",
                },
                {
                    "CUSTOMER_ID": 102,
                    "EMAIL": "bob@example.com",
                    "UPDATED_AT": "2026-10-09 10:00:00",
                },
            ]

        def fetch_all_rows(self, database: str, schema: str, table: str) -> list[dict[str, Any]]:
            return [
                {"CUSTOMER_ID": 101, "EMAIL": "alice@example.com"},
                {"CUSTOMER_ID": 102, "EMAIL": "bob@example.com"},
            ]

        def execute_query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [
                {"ORDER_ID": 901, "AMOUNT": 500, "CUSTOMER_ID": 101},
                {"ORDER_ID": 902, "AMOUNT": 1200, "CUSTOMER_ID": 102},
            ]

    return MockClient()
