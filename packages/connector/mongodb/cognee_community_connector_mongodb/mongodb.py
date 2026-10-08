"""MongoDB connector for cognee — a ``dlt`` resource that syncs a collection into memory.

Pulls documents out of a MongoDB collection into cognee memory, incrementally and
with forget-on-delete. It builds entirely on cognee's existing DLT ingestion
subsystem, so no core change is needed::

    import cognee
    from cognee_community_connector_mongodb import mongodb_source

    await cognee.remember(
        mongodb_source(
            uri="mongodb://localhost:27017",
            database="support",
            collection="tickets",
            text_fields=["subject", "body"],
            title_field="subject",
        ),
        dataset_name="my_tickets",
        primary_key="id",
        write_disposition="merge",  # REQUIRED: upsert by document id
    )

Design
------
* **Auth** — a standard MongoDB connection URI, passed as ``uri`` or read from
  ``MONGODB_URI``. Access is read-only: the connector only issues ``find``.
* **Identity** — the document ``_id``, stringified, is the row's primary key, so
  re-syncing a document upserts rather than duplicates it.
* **Ingestion path** — the resource declares
  ``cognee_document_source = "mongodb"`` (``DOCUMENT_SOURCE_ATTR``), so
  ``resolve_dlt_sources`` tags each row ``system_metadata["source"] = "mongodb"``.
  Each row therefore becomes a text document that flows through normal cognify
  entity extraction, which is the right treatment for prose, instead of the
  deterministic dlt-row schema-context path.
* **Document mapping** — MongoDB is schemaless, so the mapping is explicit rather
  than inferred. ``text_fields`` names the fields that become the document text,
  in order; ``title_field`` names the one used as the heading. Fields that are not
  named are dropped, so a metadata-only write (a view counter, a ``lastSeenAt``
  bump) does not churn the content-hash ``data_id`` downstream.
* **Incremental cursor** — ``cursor_field`` (default ``updatedAt``) is compared
  server-side with ``$gt`` and the high-water mark is persisted in dlt's
  per-resource state, so a re-run fetches and re-embeds only the delta.
* **Forget-on-delete** — MongoDB only reports deletions through change streams,
  which require a replica set and a retained oplog, so this connector does not
  depend on them. Each run diffs a cheap ``_id``-only sweep against the ids seen
  on the previous run and emits the ``_deleted`` hard-delete markers that dlt
  removes from the destination; cognee's existing ``orphan_cleanup`` then purges
  the rows from the graph, vector, and relational stores.

Failure posture
---------------
Deletion detection trusts the id sweep to enumerate every current document. An
empty sweep over a previously populated corpus almost always means a transient
failure (a dropped connection, the wrong database after a config edit, a
collection mid-restore) rather than a genuine wipe, so deletion is skipped for
that run and the id state is preserved. Treating it as "everything was deleted"
would purge the dataset and overwrite the id state, making the loss permanent.

Known limitations
-----------------
* A document with no ``cursor_field`` is ingested when first seen, but later
  *edits* to it are invisible: ``$gt`` can only match documents that carry the
  field. Set ``cursor_field="_id"`` for insert-only collections, or have writers
  maintain a timestamp.
* Wiping the entire collection upstream does not forget anything, because that is
  indistinguishable from the transient-failure case above. It self-heals: the
  next run that sees any document reconciles normally.
* The id sweep and the persisted id set are both O(documents) per run, which is
  cheap for tens of thousands of documents and wasteful for millions. Pass
  ``detect_deletions=False`` to skip it (at the cost of missing deletions and
  back-dated inserts).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("mongodb_connector")

# dlt resource / staging-table name for MongoDB documents.
MONGODB_TABLE_NAME = "mongodb_documents"
MONGODB_SOURCE_NAME = "mongodb"

# Cap on how many ids go into one ``$in`` query, so a large first-sight batch
# cannot build an unbounded query document.
_ID_FETCH_CHUNK = 500

_EXTRA_HINT = (
    'The MongoDB connector needs dlt and pymongo: pip install "cognee-community-connector-mongodb".'
)


# ---------------------------------------------------------------------------
# Document mapping
# ---------------------------------------------------------------------------
def _is_scalar(value: Any) -> bool:
    """True for a value the fallback renderer can print inline."""
    return isinstance(value, (str, int, float, bool))


def _field_text(value: Any) -> str:
    """Render one named field as text.

    Scalars go through ``str``; containers are rendered as JSON so the text cognee
    cognifies is machine-shaped rather than a Python ``repr``. ``default=str``
    covers the BSON types JSON cannot encode (ObjectId, datetime, Decimal128).
    """
    if isinstance(value, str):
        return value
    if _is_scalar(value):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _render(
    document: dict,
    text_fields: list[str] | None,
    cursor_field: str,
    title_field: str | None = None,
) -> str:
    """Render a Mongo document to the text cognee will cognify.

    With ``text_fields`` the named fields are emitted in order, skipping any that
    are absent or empty, so the body only changes when the named content changes.
    Without it, every top-level scalar is rendered as a ``key: value`` line —
    ``_id``, the cursor field, and the title field are excluded, the title because
    cognee already prefixes it as the document heading.
    """
    if text_fields:
        parts = [
            _field_text(document[field])
            for field in text_fields
            if document.get(field) not in (None, "")
        ]
        return "\n\n".join(parts)

    excluded = {"_id", cursor_field}
    if title_field:
        excluded.add(title_field)

    return "\n".join(
        f"{key}: {value}"
        for key, value in document.items()
        if key not in excluded and _is_scalar(value)
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

    The document-mode row contract is ``{id, title, content}`` plus provenance.
    Only identity, provenance, and the rendered text are kept — see the module
    docstring on why the raw document is not carried through wholesale.
    """
    title = _field_text(document[title_field]) if title_field and document.get(title_field) else ""

    return {
        "id": str(document.get("_id")),
        "database": database,
        "collection": collection,
        "title": title,
        "content": _render(document, text_fields, cursor_field, title_field),
        # Present on every live row so dlt infers the column; deletions are
        # emitted separately with _deleted=True.
        "_deleted": False,
    }


def _deleted_row(document_id: str) -> dict[str, Any]:
    """Build the hard-delete marker row for a document that vanished upstream."""
    return {"id": document_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------
def _advance_cursor(newest: Any, value: Any) -> Any:
    """Return the newer of two cursor values, tolerating incomparable types.

    A collection whose cursor field mixes types (an int timestamp next to an ISO
    string, say) would otherwise raise ``TypeError`` mid-stream and abort the whole
    sync. Keeping the old high-water mark is the safe answer: the next run re-reads
    a slightly wider window instead of losing documents.
    """
    if value is None:
        return newest
    if newest is None:
        return value
    try:
        return max(newest, value)
    except TypeError:
        logger.warning(
            "MongoDB: cursor values are not mutually comparable (%r vs %r); "
            "keeping the previous high-water mark.",
            type(newest).__name__,
            type(value).__name__,
        )
        return newest


def _with_cursor_field(projection: dict | None, cursor_field: str) -> dict | None:
    """Return a projection guaranteed to carry ``cursor_field``.

    Without the cursor field in the returned documents the high-water mark can
    never advance, so every run would re-fetch a monotonically growing delta.
    MongoDB forbids mixing inclusion and exclusion in one projection, so an
    exclusion projection that drops the cursor field is a configuration error
    rather than something to patch up.
    """
    if projection is None:
        return None

    is_exclusion = any(value == 0 for value in projection.values())
    if cursor_field in projection:
        return projection
    if is_exclusion:
        raise ValueError(
            f"projection excludes {cursor_field!r}, which the incremental cursor needs. "
            f"Drop the exclusion or pass cursor_field={cursor_field!r}."
        )
    return {**projection, cursor_field: 1}


def _chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
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
    detect_deletions: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents, then hard-delete markers for the ones that vanished.

    ``state`` is dlt's per-resource state dict and carries ``last_cursor`` (the
    ``cursor_field`` high-water mark) and ``known_ids`` (the ids seen on the
    previous run) across runs. It is mutated in place.
    """
    base_filter = dict(query_filter or {})
    read_projection = _with_cursor_field(projection, cursor_field)
    last_cursor = state.get("last_cursor")
    known_ids: set[str] = set(state.get("known_ids") or [])

    # 1. Sweep current ids. Projection-only, so MongoDB answers from the _id
    #    index without reading the documents themselves.
    current_ids: dict[str, Any] = {}
    if detect_deletions:
        for document in collection_handle.find(base_filter, {"_id": 1}):
            current_ids[str(document.get("_id"))] = document.get("_id")

    # 2. Fetch the changed set.
    newest_cursor = last_cursor
    changed = 0
    seen_this_run: set[str] = set()

    def _emit(document: dict) -> dict[str, Any] | None:
        nonlocal newest_cursor, changed
        document_id = str(document.get("_id"))
        if document_id in seen_this_run:
            return None
        seen_this_run.add(document_id)
        newest_cursor = _advance_cursor(newest_cursor, document.get(cursor_field))
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
        # First run: full backfill of everything matching the filter. The cursor
        # is left at None unless a document carries it, so the next run re-reads
        # the window rather than trusting a cursor nothing wrote.
        for document in collection_handle.find(base_filter, read_projection):
            row = _emit(document)
            if row is not None:
                yield row
    else:
        changed_filter = {**base_filter, cursor_field: {"$gt": last_cursor}}
        for document in collection_handle.find(changed_filter, read_projection):
            row = _emit(document)
            if row is not None:
                yield row

        # A document new to the corpus is fetched regardless of its cursor value,
        # so a restored or back-dated document is not lost. The $gt pass above has
        # already covered the common case; this picks up the rest.
        if detect_deletions:
            missing = sorted(set(current_ids) - known_ids - seen_this_run)
            raw_missing = [current_ids[document_id] for document_id in missing]
            for chunk in _chunked(raw_missing, _ID_FETCH_CHUNK):
                for document in collection_handle.find({"_id": {"$in": chunk}}, read_projection):
                    row = _emit(document)
                    if row is not None:
                        yield row

    # 3. Deletions. An empty sweep over a previously populated corpus is treated as
    #    a transient failure rather than a mass deletion, so state is preserved.
    if not detect_deletions:
        state["last_cursor"] = newest_cursor
        logger.info("MongoDB: %d changed document(s), deletion detection disabled.", changed)
        return

    if known_ids and not current_ids:
        logger.warning(
            "MongoDB: id sweep returned 0 documents but %d were known; skipping deletion "
            "this run so a transient failure cannot purge the dataset.",
            len(known_ids),
        )
        state["last_cursor"] = newest_cursor
        logger.info("MongoDB: %d changed document(s), 0 deletion(s).", changed)
        return

    deleted = known_ids - set(current_ids)
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
    detect_deletions: bool = True,
    client: Any = None,
):
    """Return a ``dlt`` resource yielding MongoDB documents for ``cognee.remember``.

    Args:
        database: Database name.
        collection: Collection name.
        uri: MongoDB connection URI. Falls back to ``MONGODB_URI``.
        query_filter: Optional Mongo filter restricting which documents sync. It
            also narrows the id sweep, so a document that falls out of the filter
            is treated as absent and forgotten — useful for a soft-delete flag such
            as ``{"status": {"$ne": "archived"}}``.
        projection: Optional Mongo projection for the document reads. It is forced
            to carry ``cursor_field``; an exclusion projection that drops it is
            rejected.
        text_fields: Fields, in order, that make up the document text. When omitted,
            every top-level scalar except ``_id``, ``cursor_field``, and
            ``title_field`` is rendered as a ``key: value`` line.
        title_field: Field used as the document heading.
        cursor_field: Field carrying the incremental high-water mark.
        detect_deletions: When False, skip the per-run id sweep. Cheaper, but
            deletions and back-dated inserts are then never detected.
        client: Pre-built ``pymongo.MongoClient`` (mainly a test-injection point);
            when omitted one is built from the URI above.

    Returns:
        A ``dlt`` resource (``mongodb_documents``) configured with
        ``primary_key="id"``, ``write_disposition="merge"``, and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None and not (uri or os.environ.get("MONGODB_URI")):
        raise ValueError("MongoDB connection URI required: pass uri= or set MONGODB_URI.")

    # Fail before the first query rather than on the second sync.
    _with_cursor_field(projection, cursor_field)

    @dlt.resource(
        name=MONGODB_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which is what propagates a
        # deletion upstream into cognee's orphan_cleanup.
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

        yield from sync_documents(
            handle[database][collection],
            dlt.current.resource_state(),
            database=database,
            collection=collection,
            query_filter=query_filter,
            projection=projection,
            text_fields=text_fields,
            title_field=title_field,
            cursor_field=cursor_field,
            detect_deletions=detect_deletions,
        )

    resource = mongodb_documents
    # Opt into the document ingestion path: each row becomes a text document that
    # goes through normal cognify rather than the deterministic dlt-row path.
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, MONGODB_SOURCE_NAME)
    return resource
