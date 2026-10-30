"""Unit tests for the MongoDB connector.

PyMongo is fully mocked via ``FakeCollection`` — no server and no live
credentials are required, so these run in CI. Coverage:

  - explicit text_fields mapping renders only the named fields, in order
  - the schemaless fallback renders top-level scalars and skips _id/cursor
  - full backfill yields every document and records the cursor + id set
  - incremental re-sync yields ONLY documents modified since the cursor
  - a document new to the corpus is fetched even when its cursor value is old
  - documents that vanish from the sweep become hard-delete markers
  - an empty sweep does NOT mass-delete a previously populated corpus
  - query_filter narrows the sweep, so filtered-out documents are forgotten
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a full edit/insert/delete cycle against mongomock, an independent
    implementation of MongoDB query semantics (so the $gt / $in / projection
    shapes are not merely re-confirming FakeCollection's own assumptions)
  - a real dlt merge physically removes a hard-deleted row (end-to-end)

The last link in the chain — ``orphan_cleanup`` purging the graph + vector
stores — is cognee's own tested behavior and needs a live LLM, so it is out of
scope here. These tests cover everything up to it: the connector emits the
markers, and dlt acts on them.
"""

import pytest

from cognee_community_connector_mongodb.mongodb import (
    _render,
    mongodb_source,
    sync_documents,
)


# ---------------------------------------------------------------------------
# Fake pymongo collection
# ---------------------------------------------------------------------------
def _doc(doc_id, *, updated_at=None, **fields):
    document = {"_id": doc_id}
    if updated_at is not None:
        document["updatedAt"] = updated_at
    document.update(fields)
    return document


class FakeCollection:
    """Minimal stand-in for a pymongo Collection.

    Supports the exact query shapes the connector issues: an optional base
    filter of equality terms, a ``{"$gt": value}`` term on the cursor field, and
    an ``{"_id": {"$in": [...]}}`` term. ``find`` records every call so tests can
    assert the connector pushed work down to the server instead of filtering in
    Python.
    """

    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    def find(self, query_filter=None, projection=None):
        self.calls.append((dict(query_filter or {}), projection))
        for document in self.documents:
            if not self._matches(document, query_filter or {}):
                continue
            if projection == {"_id": 1}:
                yield {"_id": document["_id"]}
            else:
                yield dict(document)

    @staticmethod
    def _matches(document, query_filter):
        for key, condition in query_filter.items():
            if isinstance(condition, dict):
                if "$gt" in condition:
                    value = document.get(key)
                    if value is None or not value > condition["$gt"]:
                        return False
                if "$in" in condition and document.get(key) not in condition["$in"]:
                    return False
            elif document.get(key) != condition:
                return False
        return True


def _run(collection, state, **kwargs):
    """Drain sync_documents into (live_rows, deleted_ids)."""
    rows = list(
        sync_documents(
            collection,
            state,
            database="testdb",
            collection="testcol",
            **kwargs,
        )
    )
    live = [row for row in rows if not row.get("_deleted")]
    deleted = [row["id"] for row in rows if row.get("_deleted")]
    return live, deleted


# ---------------------------------------------------------------------------
# Document mapping
# ---------------------------------------------------------------------------
def test_text_fields_render_in_order_and_skip_missing():
    document = _doc("1", subject="Login broken", body="Cannot sign in", noise="ignore me")
    rendered = _render(document, ["subject", "missing", "body"], "updatedAt")
    assert rendered == "Login broken\n\nCannot sign in"
    assert "ignore me" not in rendered


def test_fallback_renders_scalars_and_skips_id_and_cursor():
    document = _doc("1", updated_at=5, subject="Hi", count=3, nested={"a": 1})
    rendered = _render(document, None, "updatedAt")
    assert "subject: Hi" in rendered
    assert "count: 3" in rendered
    # _id, the cursor field, and non-scalars are excluded.
    assert "_id" not in rendered
    assert "updatedAt" not in rendered
    assert "nested" not in rendered


# ---------------------------------------------------------------------------
# Ingest path
# ---------------------------------------------------------------------------
def test_full_backfill_yields_everything_and_records_state():
    collection = FakeCollection(
        [
            _doc("a", updated_at=1, subject="First"),
            _doc("b", updated_at=3, subject="Second"),
        ]
    )
    state = {}

    live, deleted = _run(collection, state, text_fields=["subject"])

    assert {row["id"] for row in live} == {"a", "b"}
    assert deleted == []
    assert state["last_cursor"] == 3
    assert state["known_ids"] == ["a", "b"]
    # Rows carry provenance and the rendered text.
    assert live[0]["database"] == "testdb"
    assert live[0]["collection"] == "testcol"
    assert live[0]["content"] == "First"


def test_title_field_is_used_for_the_row_title():
    collection = FakeCollection([_doc("a", updated_at=1, subject="Hello", body="World")])
    live, _ = _run(collection, {}, text_fields=["body"], title_field="subject")
    assert live[0]["title"] == "Hello"
    assert live[0]["content"] == "World"


# ---------------------------------------------------------------------------
# Incremental cursor
# ---------------------------------------------------------------------------
def test_incremental_yields_only_changed_documents():
    collection = FakeCollection(
        [
            _doc("a", updated_at=1, subject="Old"),
            _doc("b", updated_at=5, subject="New"),
        ]
    )
    state = {"last_cursor": 3, "known_ids": ["a", "b"]}

    live, deleted = _run(collection, state, text_fields=["subject"])

    assert [row["id"] for row in live] == ["b"]
    assert deleted == []
    assert state["last_cursor"] == 5


def test_incremental_pushes_the_cursor_down_to_the_server():
    collection = FakeCollection([_doc("a", updated_at=1)])
    _run(collection, {"last_cursor": 3, "known_ids": ["a"]})

    # The sweep is projection-only; the document read carries the $gt term.
    document_reads = [call for call in collection.calls if call[1] != {"_id": 1}]
    assert any(call[0].get("updatedAt") == {"$gt": 3} for call in document_reads)


def test_document_new_to_the_corpus_is_fetched_despite_an_old_cursor():
    # "c" was restored from a backup: its updatedAt predates the cursor, but the
    # connector has never seen its id before.
    collection = FakeCollection(
        [
            _doc("a", updated_at=1, subject="Known"),
            _doc("c", updated_at=2, subject="Restored"),
        ]
    )
    state = {"last_cursor": 4, "known_ids": ["a"]}

    live, deleted = _run(collection, state, text_fields=["subject"])

    assert [row["id"] for row in live] == ["c"]
    assert deleted == []
    assert state["known_ids"] == ["a", "c"]


def test_a_document_is_not_emitted_twice_in_one_run():
    # "b" matches both the $gt pass and the new-id backfill pass.
    collection = FakeCollection([_doc("b", updated_at=9, subject="New")])
    state = {"last_cursor": 1, "known_ids": []}

    live, _ = _run(collection, state, text_fields=["subject"])

    assert [row["id"] for row in live] == ["b"]


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_vanished_document_becomes_a_hard_delete_marker():
    collection = FakeCollection([_doc("a", updated_at=1)])
    state = {"last_cursor": 1, "known_ids": ["a", "gone"]}

    live, deleted = _run(collection, state)

    assert deleted == ["gone"]
    assert state["known_ids"] == ["a"]
    assert all(row["_deleted"] is False for row in live)


def test_empty_sweep_does_not_mass_delete():
    collection = FakeCollection([])
    state = {"last_cursor": 7, "known_ids": ["a", "b", "c"]}

    live, deleted = _run(collection, state)

    assert live == []
    assert deleted == []
    # State is preserved so the loss cannot become permanent.
    assert state["known_ids"] == ["a", "b", "c"]


def test_query_filter_narrows_the_sweep_so_excluded_documents_are_forgotten():
    collection = FakeCollection(
        [
            _doc("a", updated_at=1, status="open"),
            _doc("b", updated_at=1, status="archived"),
        ]
    )
    state = {"last_cursor": 1, "known_ids": ["a", "b"]}

    live, deleted = _run(collection, state, query_filter={"status": "open"})

    # The archived document is forgotten, and it is not re-ingested as a live row.
    assert deleted == ["b"]
    assert [row["id"] for row in live] == []
    assert state["known_ids"] == ["a"]


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_source_requires_a_uri_when_no_client_is_injected(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(ValueError, match="connection URI required"):
        mongodb_source(database="d", collection="c")


def test_resource_is_wired_for_merge_with_a_hard_delete_column():
    dlt = pytest.importorskip("dlt")
    assert dlt  # imported for the resource decorator

    class FakeClient:
        def __getitem__(self, _name):
            return self

    resource = mongodb_source(
        database="testdb",
        collection="testcol",
        client=FakeClient(),
    )

    table = resource.compute_table_schema()
    assert table["write_disposition"] == "merge"
    assert table["columns"]["_deleted"]["hard_delete"] is True
    assert "id" in [name for name, col in table["columns"].items() if col.get("primary_key")]


# ---------------------------------------------------------------------------
# Against mongomock (an independent implementation of MongoDB query semantics,
# so these do not merely re-confirm the assumptions baked into FakeCollection)
# ---------------------------------------------------------------------------
def _mongomock_collection(documents):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    collection = client["testdb"]["testcol"]
    if documents:
        collection.insert_many(documents)
    return client, collection


def test_full_cycle_against_mongomock():
    """Backfill, then an edit, an insert and a delete — on real Mongo semantics."""
    _client, collection = _mongomock_collection(
        [
            {"_id": "a", "updatedAt": 1, "subject": "Alpha"},
            {"_id": "b", "updatedAt": 2, "subject": "Beta"},
        ]
    )
    state = {}

    live, deleted = _run(collection, state, text_fields=["subject"])
    assert {row["id"] for row in live} == {"a", "b"}
    assert deleted == []
    assert state["last_cursor"] == 2

    # Edit "a", insert "c", delete "b".
    collection.update_one({"_id": "a"}, {"$set": {"subject": "Alpha v2", "updatedAt": 9}})
    collection.insert_one({"_id": "c", "updatedAt": 3, "subject": "Gamma"})
    collection.delete_one({"_id": "b"})

    live, deleted = _run(collection, state, text_fields=["subject"])

    # "a" changed (cursor), "c" is new (id sweep); "b" is gone.
    assert {row["id"] for row in live} == {"a", "c"}
    assert {row["content"] for row in live} == {"Alpha v2", "Gamma"}
    assert deleted == ["b"]
    assert state["last_cursor"] == 9
    assert state["known_ids"] == ["a", "c"]

    # A third run with nothing changed yields nothing at all.
    live, deleted = _run(collection, state, text_fields=["subject"])
    assert live == []
    assert deleted == []


def test_projection_is_honored_against_mongomock():
    _client, collection = _mongomock_collection(
        [{"_id": "a", "updatedAt": 1, "subject": "Keep", "secret": "drop me"}]
    )
    live, _ = _run(
        collection,
        {},
        projection={"_id": 1, "subject": 1, "updatedAt": 1},
        text_fields=None,
    )
    # The projected-away field never reaches the rendered row.
    assert "drop me" not in live[0]["content"]
    assert "subject: Keep" in live[0]["content"]


# ---------------------------------------------------------------------------
# End-to-end: a delete marker physically removes the row via a real dlt merge
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")
    _client, collection = _mongomock_collection(
        [
            {"_id": "a", "updatedAt": 1, "subject": "Alpha"},
            {"_id": "b", "updatedAt": 2, "subject": "Beta"},
        ]
    )

    pipeline = dlt.pipeline(
        pipeline_name="test_mongodb_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "mongodb.duckdb")),
        dataset_name="mongo",
    )

    def source():
        return mongodb_source(
            database="testdb",
            collection="testcol",
            client=_client,
            text_fields=["subject"],
        )

    # Sync #1: both documents land in the destination.
    pipeline.run(source())
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM mongodb_documents")[0][0] == 2

    # Sync #2: "b" is deleted upstream. The connector emits a hard-delete marker
    # and dlt's merge physically removes the row.
    collection.delete_one({"_id": "b"})
    pipeline.run(source())
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM mongodb_documents")
    assert [r[0] for r in rows] == ["a"]


def test_resource_is_marked_for_the_document_ingestion_path():
    """The marker routes rows through cognify rather than the dlt-row path."""
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    class FakeClient:
        def __getitem__(self, _name):
            return self

    resource = mongodb_source(database="testdb", collection="testcol", client=FakeClient())
    assert getattr(resource, DOCUMENT_SOURCE_ATTR, None) == "mongodb"
