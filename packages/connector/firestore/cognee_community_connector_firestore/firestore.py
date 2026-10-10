
"""Google Cloud Firestore connector for Cognee."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("firestore_connector")


def _normalise_value(value: Any) -> Any:
    """Convert Firestore values into JSON-friendly Python values."""
    from google.cloud.firestore_v1 import DocumentReference

    if isinstance(value, DocumentReference):
        return {
            "reference_path": value.path,
            "reference_type": "firestore_document",
        }

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {key: _normalise_value(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_normalise_value(item) for item in value]

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _document_to_row(
    document: Any,
    collection_name: str,
) -> dict[str, Any]:
    """Convert a Firestore document snapshot into a Cognee document row."""
    data = _normalise_value(document.to_dict() or {})
    document_path = document.reference.path

    return {
        "id": document_path,
        "title": document.id,
        "content": (
            f"Firestore document: {document_path}\n"
            f"Collection: {collection_name}\n"
            f"Data: {data}"
        ),
        "url": None,
        "document_path": document_path,
        "collection": collection_name,
        "updated_at": (
            document.update_time.isoformat()
            if document.update_time
            else None
        ),
        "_deleted": False,
    }


def firestore_source(
    collection: str,
    *,
    project_id: str | None = None,
    timestamp_field: str | None = None,
    client: Any = None,
):
    """Create a dlt source for a Firestore collection.

    Without timestamp_field, each successful sync produces a full snapshot
    using replace semantics.

    With timestamp_field, changed documents are detected by comparing the
    field value with the previous sync state. Deleted documents are emitted
    as hard-delete markers.

    Authentication uses Google Application Default Credentials unless a
    Firestore client is injected.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'Install the connector dependencies with '
            'pip install -e "packages/connector/firestore[dev]"'
        ) from exc

    if not collection or not collection.strip():
        raise ValueError("collection must be a non-empty collection name.")

    if timestamp_field is not None and not timestamp_field.strip():
        raise ValueError("timestamp_field cannot be empty.")

    resolved_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")

    # A full snapshot handles documents disappearing from the collection.
    # Timestamp mode uses merge and explicit deletion markers.
    write_disposition = "merge" if timestamp_field else "replace"

    @dlt.resource(
        name="firestore_documents",
        write_disposition=write_disposition,
        primary_key="id",
        columns={
            "_deleted": {
                "data_type": "bool",
                "hard_delete": True,
            }
        }
        if timestamp_field
        else None,
    )
    def firestore_documents():
        from google.cloud import firestore

        db = client or firestore.Client(project=resolved_project)
        snapshots = db.collection(collection).stream()

        # Without a timestamp field, emit every document. The full snapshot
        # lets replace semantics remove documents absent from this sync.
        if not timestamp_field:
            count = 0

            for snapshot in snapshots:
                yield _document_to_row(snapshot, collection)
                count += 1

            logger.info(
                "Firestore collection %s: full snapshot yielded %d document(s).",
                collection,
                count,
            )
            return

        # Timestamp mode compares document versions with the previous state.
        state = dlt.current.resource_state()
        previous = state.get("documents", {})
        current: dict[str, str] = {}
        changed_count = 0
        deleted_count = 0

        for snapshot in snapshots:
            row = _document_to_row(snapshot, collection)
            document_id = row["id"]
            data = snapshot.to_dict() or {}
            raw_timestamp = data.get(timestamp_field)

            if raw_timestamp is None:
                raise ValueError(
                    f"Document {document_id!r} is missing timestamp field "
                    f"{timestamp_field!r}."
                )

            timestamp_value = (
                raw_timestamp.isoformat()
                if isinstance(raw_timestamp, datetime)
                else str(raw_timestamp)
            )
            current[document_id] = timestamp_value

            if previous.get(document_id) != timestamp_value:
                yield row
                changed_count += 1

        # Emit hard-delete markers for documents removed since the last scan.
        deleted_ids = set(previous) - set(current)

        for document_id in sorted(deleted_ids):
            yield {"id": document_id, "_deleted": True}
            deleted_count += 1

        # Save state after the collection scan and row generation complete.
        state["documents"] = current

        logger.info(
            "Firestore collection %s: %d changed document(s), "
            "%d deletion(s).",
            collection,
            changed_count,
            deleted_count,
        )

    @dlt.source(name="firestore")
    def _firestore():
        return firestore_documents

    source = _firestore()
    setattr(source, DOCUMENT_SOURCE_ATTR, "firestore")
    return source

