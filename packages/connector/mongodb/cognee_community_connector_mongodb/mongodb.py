"""MongoDB connector for cognee — a ``dlt`` source that turns a collection into memory.

Pull documents out of a MongoDB collection into cognee, incrementally and with
forget-on-deletion. Like the sibling Confluence connector this builds entirely on
the existing DLT ingestion subsystem; the source produced here is handed directly
to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_mongodb import mongodb_source

    await cognee.remember(
        mongodb_source(
            uri="mongodb://localhost:27017",
            database="support",
            collection="tickets",
            text_fields=["subject", "body"],
        ),
        dataset_name="my_tickets",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by document id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — a standard MongoDB connection URI, passed as ``uri`` or read from
  ``MONGODB_URI``. Access is read-only; the connector only issues ``find``.
* **Primary key** — the document ``_id``, stringified. Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Ingestion path** — the source declares ``cognee_document_source = "mongodb"``,
  so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] =
  "mongodb"`` (not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and
  each document flows through the standard cognify entity-extraction pipeline —
  the right treatment for the rendered prose — instead of the deterministic
  dlt-row schema-context path.
* **Document mapping** — MongoDB is schemaless, so the mapping is explicit
  rather than inferred. ``text_fields`` names the fields that become the
  document text, in order; ``title_field`` names the one used as a heading.
  Anything not named is dropped, which keeps a metadata-only write (a counter, a
  ``lastSeenAt`` bump) from churning the content-hash ``data_id`` downstream.
  With no ``text_fields`` the connector falls back to every top-level scalar
  field except ``_id`` and the cursor field, rendered as ``key: value`` lines.
* **Incremental cursor** — ``cursor_field`` (default ``updatedAt``) is compared
  server-side with ``$gt``, and the high-water mark is persisted in dlt's
  per-resource state, so re-running ``remember`` resumes where it left off and
  re-embeds only the delta. Documents that are new to the corpus are always
  fetched even when their cursor value is old, so a restored or back-dated
  document is not missed.
* **Forget-on-delete** — MongoDB exposes deletions only through change streams,
  which require a replica set and a retained oplog, so this connector does not
  depend on them. Each run instead does a lightweight ``_id``-only sweep (a
  covered index scan) and compares it against the ids seen on the previous run,
  also kept in resource state. Documents that vanished are emitted with the
  ``_deleted`` hard-delete marker; dlt removes those rows on ``merge`` and
  cognee's existing ``orphan_cleanup`` then purges them from the graph + vector
  + relational stores. No parallel cleanup path.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows from
   the dlt destination (default 50). For a real collection pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. note::
   A document whose ``cursor_field`` is missing is ingested on the run that first
   sees it (via the id sweep), but later *edits* to it cannot be detected — the
   ``$gt`` filter can only match documents that carry the field. Set
   ``cursor_field="_id"`` for insert-only collections, or ensure writers maintain
   the timestamp.
"""

from __future__ import annotations

import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("mongodb_connector")

# dlt resource / staging-table name for MongoDB documents.
MONGODB_TABLE_NAME = "mongodb_documents"
MONGODB_SOURCE_NAME = "mongodb"

# Chunk size for the ``$in`` backfill of documents new to the corpus, so a large
# first-sight batch cannot build an unbounded query document.
_ID_CHUNK = 500

_EXTRA_HINT = (
    'The MongoDB connector requires the "mongodb" extra: pip install '
    '"cognee-community-connector-mongodb" (provides dlt and pymongo).'
)


# ---------------------------------------------------------------------------
# Document mapping (module-private)
# ---------------------------------------------------------------------------
def _scalar(value: Any) -> bool:
    """True for values worth rendering into the fallback text body."""
    return isinstance(value, (str, int, float, bool))


def _render(document: dict, text_fields: list[str] | None, cursor_field: str) -> str:
    """Render a Mongo document to the text cognee will cognify.

    With ``text_fields`` the named fields are emitted in order, skipping any that
    are absent or empty, so the body stays stable when an unrelated field
    changes. Without it, every top-level scalar except ``_id`` and the cursor
    field is rendered as a ``key: value`` line.
    """
    if text_fields:
        parts = []
        for field in text_fields:
            value = document.get(field)
            if value is None or value == "":
                continue
            parts.append(str(value))
        return "\n\n".join(parts)

    return "\n".join(
        f"{key}: {value}"
        for key, value in document.items()
        if key not in ("_id", cursor_field) and _scalar(value)
    )


def _document_to_row(
    document: dict,
    *,
    database: str,
    collection: str,
    text_fields: list[str] | None,
    title_field: str | None,
    cursor_field: str,
) -> dict[str, Any]:
    """Flatten a Mongo document into a dlt row.

    Document-mode row contract: ``{id, title, content}`` plus provenance.
    ``resolve_dlt_sources`` tags these rows
    ``external_metadata["source"] = "mongodb"`` (see the DOCUMENT_SOURCE_ATTR
    marker on the resource), so each document flows through normal cognify
    (LLM graph extraction) rather than the relational schema path.

    Only identity, provenance and the rendered text are kept — see the module
    docstring on why the raw document is not carried through wholesale.
    """
    return {
        "id": str(document.get("_id")),
        "database": database,
        "collection": collection,
        "title": str(document.get(title_field) or "") if title_field else "",
        "content": _render(document, text_fields, cursor_field),
        # Present on every live row so dlt infers the column; deletions are
        # emitted separately with _deleted=True.
        "_deleted": False,
    }


def _deleted_row(document_id: str) -> dict[str, Any]:
    """Build the hard-delete marker row for a document that vanished upstream."""
    return {"id": str(document_id), "_deleted": True}


def _chunked(items: list[Any], size: int):
    """Yield ``items`` in lists of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync_documents(
    collection_handle: Any,
    state: dict,
    *,
    database: str,
    collection: str,
    query_filter: dict | None = None,
    projection: dict | None = None,
    text_fields: list[str] | None = None,
    title_field: str | None = None,
    cursor_field: str = "updatedAt",
):
    """Yield changed documents, then hard-delete markers for vanished ones.

    ``state`` is dlt's per-resource state dict and carries ``last_cursor`` (the
    high-water mark of ``cursor_field``) and ``known_ids`` (the id set from the
    previous run) across runs.
    """
    base_filter = dict(query_filter or {})
    known_ids: set[str] = set(state.get("known_ids") or [])
    last_cursor = state.get("last_cursor")

    # 1. Sweep current ids. Projection-only, so this is a covered index scan
    #    rather than a full document read.
    current_ids: set[str] = set()
    raw_ids: dict[str, Any] = {}
    for document in collection_handle.find(base_filter, {"_id": 1}):
        document_id = str(document.get("_id"))
        current_ids.add(document_id)
        raw_ids[document_id] = document.get("_id")

    # 2. Fetch the changed set.
    newest_cursor = last_cursor
    changed = 0
    seen_this_run: set[str] = set()

    def _emit(document: dict):
        nonlocal newest_cursor, changed
        document_id = str(document.get("_id"))
        if document_id in seen_this_run:
            return None
        seen_this_run.add(document_id)
        value = document.get(cursor_field)
        if value is not None and (newest_cursor is None or value > newest_cursor):
            newest_cursor = value
        changed += 1
        return _document_to_row(
            document,
            database=database,
            collection=collection,
            text_fields=text_fields,
            title_field=title_field,
            cursor_field=cursor_field,
        )

    if last_cursor is None:
        # First run: full backfill of everything matching the filter.
        for document in collection_handle.find(base_filter, projection):
            row = _emit(document)
            if row is not None:
                yield row
    else:
        changed_filter = dict(base_filter)
        changed_filter[cursor_field] = {"$gt": last_cursor}
        for document in collection_handle.find(changed_filter, projection):
            row = _emit(document)
            if row is not None:
                yield row

        # Documents new to the corpus are fetched regardless of their cursor
        # value, so a restored or back-dated document is not lost. The $gt pass
        # above has already covered most of them; this only picks up the rest.
        missing = sorted(current_ids - known_ids - seen_this_run)
        for chunk in _chunked([raw_ids[document_id] for document_id in missing], _ID_CHUNK):
            for document in collection_handle.find({"_id": {"$in": chunk}}, projection):
                row = _emit(document)
                if row is not None:
                    yield row

    # 3. Deletion detection relies on the sweep enumerating every current
    #    document. An empty sweep while documents were previously known almost
    #    always means a transient failure (a dropped connection, the wrong
    #    database after a config edit, a collection mid-restore) rather than a
    #    genuine wipe — treating it as "all deleted" would purge the whole
    #    dataset and overwrite known_ids with [], making the loss permanent.
    #    Skip deletion and preserve state in that case.
    if known_ids and not current_ids:
        logger.warning(
            "MongoDB: document sweep returned 0 documents but %d were known; skipping "
            "deletion this run to avoid a mass forget-on-delete on a transient sweep.",
            len(known_ids),
        )
        state["last_cursor"] = newest_cursor
        logger.info("MongoDB: %d changed document(s), 0 deletion(s).", changed)
        return

    deleted = known_ids - current_ids
    for document_id in sorted(deleted):
        yield _deleted_row(document_id)

    state["known_ids"] = sorted(current_ids)
    state["last_cursor"] = newest_cursor
    logger.info("MongoDB: %d changed document(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def mongodb_source(
    *,
    database: str,
    collection: str,
    uri: str | None = None,
    query_filter: dict | None = None,
    projection: dict | None = None,
    text_fields: list[str] | None = None,
    title_field: str | None = None,
    cursor_field: str = "updatedAt",
    client: Any = None,
):
    """Return a ``dlt`` resource that yields MongoDB documents for ``remember``.

    Args:
        database: Database name.
        collection: Collection name.
        uri: MongoDB connection URI. Falls back to ``MONGODB_URI``.
        query_filter: Optional Mongo filter restricting which documents sync.
            Applied to the id sweep as well, so documents outside it are treated
            as absent and are forgotten rather than silently retained.
        projection: Optional Mongo projection for the document reads. The id
            sweep always uses its own ``{"_id": 1}`` projection.
        text_fields: Fields, in order, that make up the document text. When
            omitted every top-level scalar except ``_id`` and ``cursor_field``
            is rendered as ``key: value``.
        title_field: Field used as the row title.
        cursor_field: Field carrying the incremental high-water mark.
        client: Pre-built ``pymongo.MongoClient`` (mainly a test-injection
            point); when omitted one is built from the URI above.

    Returns:
        A ``dlt`` resource (``mongodb_documents``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_uri = uri or os.environ.get("MONGODB_URI")
        if not resolved_uri:
            raise ValueError("MongoDB connection URI required: pass uri= or set MONGODB_URI.")

    @dlt.resource(
        name=MONGODB_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def mongodb_documents():
        handle = client
        if handle is None:
            try:
                from pymongo import MongoClient
            except ImportError as exc:
                raise ImportError(_EXTRA_HINT) from exc
            handle = MongoClient(uri or os.environ.get("MONGODB_URI"))

        resource_state = dlt.current.resource_state()
        yield from sync_documents(
            handle[database][collection],
            resource_state,
            database=database,
            collection=collection,
            query_filter=query_filter,
            projection=projection,
            text_fields=text_fields,
            title_field=title_field,
            cursor_field=cursor_field,
        )

    resource = mongodb_documents
    # Opt into the document ingestion path: each row becomes a text document
    # that flows through normal cognify (LLM graph extraction) instead of the
    # deterministic dlt-row schema-context path — the right treatment for the
    # prose this connector renders. resolve_dlt_sources reads this marker; it
    # never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, MONGODB_SOURCE_NAME)
    return resource
