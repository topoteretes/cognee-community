
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_firestore.firestore import (
    _document_to_row,
    firestore_source,
)


def make_snapshot(
    document_id="doc-1",
    data=None,
    collection="customers",
    update_time=None,
):
    reference = SimpleNamespace(
        path=f"{collection}/{document_id}",
    )
    document_data = {"name": "Alice"} if data is None else data

    return SimpleNamespace(
        id=document_id,
        reference=reference,
        update_time=update_time or datetime(
            2026, 1, 1, tzinfo=timezone.utc
        ),
        to_dict=lambda: document_data,
    )


def test_document_to_row_contains_document_data():
    snapshot = make_snapshot(
        data={"name": "Alice", "city": "Bengaluru"}
    )

    row = _document_to_row(snapshot, "customers")

    assert row["id"] == "customers/doc-1"
    assert row["title"] == "doc-1"
    assert "Alice" in row["content"]
    assert "Bengaluru" in row["content"]
    assert row["_deleted"] is False


def test_document_to_row_handles_empty_document():
    snapshot = make_snapshot(data={})

    row = _document_to_row(snapshot, "customers")

    assert row["id"] == "customers/doc-1"
    assert row["_deleted"] is False
    assert "Data: {}" in row["content"]


def test_firestore_source_rejects_empty_collection():
    with pytest.raises(ValueError, match="collection"):
        firestore_source("  ")


def test_firestore_source_rejects_empty_timestamp_field():
    with pytest.raises(ValueError, match="timestamp_field"):
        firestore_source("customers", timestamp_field="  ")


def test_document_to_row_includes_update_time():
    update_time = datetime(2026, 5, 1, tzinfo=timezone.utc)
    snapshot = make_snapshot(update_time=update_time)

    row = _document_to_row(snapshot, "customers")

    assert row["updated_at"] == update_time.isoformat()


def test_firestore_source_uses_replace_without_timestamp():
    source = firestore_source("customers", client=MagicMock())
    resource = source.resources["firestore_documents"]

    assert resource.write_disposition == "replace"


def test_firestore_source_uses_merge_with_timestamp():
    source = firestore_source(
        "customers",
        timestamp_field="updated_at",
        client=MagicMock(),
    )
    resource = source.resources["firestore_documents"]

    assert resource.write_disposition == "merge"
