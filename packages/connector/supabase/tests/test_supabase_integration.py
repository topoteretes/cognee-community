"""Opt-in, read-only smoke test against a real Supabase database."""

import os

import pytest

from cognee_community_connector_supabase import (
    discover_supabase_schema,
    supabase_source,
)

pytestmark = pytest.mark.integration


def test_live_supabase_schema_and_source_configuration():
    database_url = os.getenv("SUPABASE_TEST_DATABASE_URL")
    project_ref = os.getenv("SUPABASE_TEST_PROJECT_REF")
    table = os.getenv("SUPABASE_TEST_TABLE")
    cursor = os.getenv("SUPABASE_TEST_CURSOR")
    if not all((database_url, project_ref, table, cursor)):
        pytest.skip("Set SUPABASE_TEST_DATABASE_URL/PROJECT_REF/TABLE/CURSOR to run.")

    schema = discover_supabase_schema(database_url)
    assert table in schema
    selected_columns = list(dict.fromkeys([*schema[table]["primary_key"], cursor]))
    source = supabase_source(
        database_url,
        project_ref=project_ref,
        tables=[table],
        columns={table: selected_columns},
        cursor_columns={table: cursor},
    )
    assert source is not None
