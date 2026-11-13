"""Unit tests for the MongoDB connector: mapping, incremental cursor, deletions, dlt wiring.

PyMongo is faked, so these need no server and no credentials. The fake records every
``find`` call so the tests can assert the connector pushed filtering to MongoDB
instead of doing it in Python, and mongomock (an independent implementation of
MongoDB query semantics) re-checks the same shapes against something other than the
tests' own fake. The final link — cognee's ``orphan_cleanup`` purging the graph and
vector stores — is covered separately in ``test_mongodb_forget.py``.
"""

from typing import Any, NamedTuple

import pytest

from cognee_community_connector_mongodb.mongodb import (
    _advance_cursor,
    _render,
    _with_cursor_field,
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

    Supports the query shapes the connector issues: a base filter of equality
    terms, a ``{"$gt": value}`` term on the cursor field, and an
    ``{"_id": {"$in": [...]}}`` term. ``find`` records every call so tests can
    assert what was pushed down to the server.
    """

    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    def find(self, query_filter=None, projection=None, hint=None):
        self.calls.append((dict(query_filter or {}), projection, hint))
        for document in self.documents:
            if not self._matches(document, query_filter or {}):
                continue
            if projection == {"_id": 1}:
                yield {"_id": document["_id"]}
            elif projection:
                yield {key: document[key] for key in projection if key in document}
            else:
                yield dict(document)

    @staticmethod
    def _matches(document, query_filter):
        for key, condition in query_filter.items():
            if isinstance(condition, dict):
                if "$gt" in condition:
                    value = document.get(key)
                    # MongoDB only compares values of the same BSON type, so a
                    # string cursor never matches a numeric $gt. Mirror that
                    # instead of letting Python raise on the comparison.
                    same_type = value is not None and type(value) is type(condition["$gt"])
                    if not same_type or not value > condition["$gt"]:
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


def test_fallback_renders_scalars_and_skips_id_cursor_and_title():
    document = _doc("1", updated_at=5, subject="Hi", count=3, nested={"a": 1})
    rendered = _render(document, None, "updatedAt", title_field="subject")
    assert "count: 3" in rendered
    # _id, the cursor field, the title field (cognee prefixes it) and containers.
    assert "_id" not in rendered
    assert "updatedAt" not in rendered
    assert "subject" not in rendered
    assert "nested" not in rendered


def test_named_container_field_is_rendered_as_json():
    document = _doc("1", updated_at=1, details={"tags": ["a", "b"], "n": 2})
    rendered = _render(document, ["details"], "updatedAt")
    assert rendered == '{"n": 2, "tags": ["a", "b"]}'
    # A Python repr would leak single quotes into the text cognee cognifies.
    assert "'tags'" not in rendered


def test_title_field_populates_the_row_title_without_duplicating_it():
    # cognee prefixes the title as the document heading, so the fallback render
    # must not also emit it as a body line.
    collection = FakeCollection([_doc("a", updated_at=1, subject="Hello", body="World")])
    live, _ = _run(collection, {}, title_field="subject")
    assert live[0]["title"] == "Hello"
    assert live[0]["content"] == "body: World"


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


def test_backfill_without_a_cursor_field_leaves_the_cursor_unset():
    # Nothing wrote a cursor, so the next run must re-read rather than trust one.
    collection = FakeCollection([_doc("a", subject="No timestamp here")])
    state = {}

    live, _ = _run(collection, state, text_fields=["subject"])

    assert [row["id"] for row in live] == ["a"]
    assert state["last_cursor"] is None


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
    _run(collection, {"last_cursor": 3, "known_ids": ["a"]}, text_fields=["subject"])

    # The sweep is projection-only; the document read carries the $gt term.
    document_reads = [call for call in collection.calls if call[1] != {"_id": 1}]
    assert any(call[0].get("updatedAt") == {"$gt": 3} for call in document_reads)


def test_id_sweep_is_hinted_to_the_id_index_when_unfiltered():
    # Measured against a real mongod: an unfiltered _id-only projection read is
    # planned as a collection scan (PROJECTION_SIMPLE + COLLSCAN) even at 50k
    # documents, so the sweep has to pin the index to get a covered scan.
    collection = FakeCollection([_doc("a", updated_at=1)])
    _run(collection, {})

    sweeps = [call for call in collection.calls if call[1] == {"_id": 1}]
    assert len(sweeps) == 1
    assert sweeps[0][2] == {"_id": 1}


def test_id_sweep_defers_to_the_planner_under_a_query_filter():
    # A selective index on the filter field beats a full _id index scan, so the
    # connector must not pin the index out from under the planner here.
    collection = FakeCollection([_doc("a", updated_at=1, status="open")])
    _run(collection, {}, query_filter={"status": "open"})

    sweeps = [call for call in collection.calls if call[1] == {"_id": 1}]
    assert len(sweeps) == 1
    assert sweeps[0][2] is None


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


def test_incomparable_cursor_values_keep_the_previous_mark():
    # A collection whose cursor field mixes types must not abort the sync.
    assert _advance_cursor(5, "2026-01-01") == 5
    assert _advance_cursor(None, 7) == 7
    assert _advance_cursor(5, None) == 5
    assert _advance_cursor(5, 9) == 9


def test_mixed_cursor_types_do_not_abort_the_sync():
    # Backfill has no $gt filter, so a collection whose cursor field mixes types
    # feeds both kinds of value into the high-water-mark comparison. It must keep
    # the previous mark rather than raise TypeError and kill the run.
    collection = FakeCollection(
        [
            _doc("a", updated_at=5, subject="Numeric"),
            _doc("b", subject="No timestamp"),
        ]
    )
    collection.documents[1]["updatedAt"] = "2026-01-01"
    state = {}

    live, _ = _run(collection, state, text_fields=["subject"])

    assert {row["id"] for row in live} == {"a", "b"}
    assert state["last_cursor"] == 5


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def test_inclusion_projection_is_forced_to_carry_the_cursor_field():
    # Without updatedAt in the projection the high-water mark can never advance,
    # so every run would re-fetch a growing delta.
    assert _with_cursor_field({"subject": 1}, "updatedAt") == {"subject": 1, "updatedAt": 1}
    assert _with_cursor_field({"subject": 1, "updatedAt": 1}, "updatedAt") == {
        "subject": 1,
        "updatedAt": 1,
    }
    assert _with_cursor_field(None, "updatedAt") is None


def test_exclusion_projection_dropping_the_cursor_is_rejected():
    with pytest.raises(ValueError, match="cursor"):
        _with_cursor_field({"secret": 0}, "updatedAt")
    # An exclusion projection that keeps the cursor field is fine.
    assert _with_cursor_field({"secret": 0, "updatedAt": 1}, "updatedAt")["secret"] == 0


def test_forced_cursor_field_reaches_the_document_reads():
    collection = FakeCollection([_doc("a", updated_at=4, subject="Keep", secret="drop")])
    state = {"last_cursor": 1, "known_ids": ["a"]}

    live, _ = _run(collection, state, projection={"subject": 1}, text_fields=["subject"])

    assert live[0]["content"] == "Keep"
    assert state["last_cursor"] == 4


def test_bad_projection_fails_before_any_query():
    collection = FakeCollection([_doc("a", updated_at=1)])
    with pytest.raises(ValueError, match="cursor"):
        list(
            sync_documents(
                collection,
                {},
                database="d",
                collection="c",
                projection={"secret": 0},
            )
        )


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
    assert live == []
    assert state["known_ids"] == ["a"]


def test_deletion_detection_can_be_turned_off():
    collection = FakeCollection([_doc("a", updated_at=1), _doc("b", updated_at=2)])
    state = {"last_cursor": 1, "known_ids": ["a", "b", "gone"]}

    live, deleted = _run(collection, state, detect_deletions=False)

    # No sweep at all, so no back-dated insert and no deletion.
    assert {row["id"] for row in live} == {"b"}
    assert deleted == []
    # The previous id set is left untouched rather than overwritten with [].
    assert state["known_ids"] == ["a", "b", "gone"]
    assert not any(call[1] == {"_id": 1} for call in collection.calls)


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_source_requires_a_uri_when_no_client_is_injected(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(ValueError, match="connection URI required"):
        mongodb_source(database="d", collection="c")


def test_source_reads_the_uri_from_the_environment(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")

    class FakeClient:
        def __getitem__(self, _name):
            return self

    # Constructed without a client: the factory resolves the URI eagerly, so a
    # missing credential fails at build time rather than mid-sync.
    assert mongodb_source(database="d", collection="c", client=FakeClient()) is not None

    # The URI alone is enough — no client injected, no ValueError.
    assert mongodb_source(database="d", collection="c") is not None


def test_bad_projection_is_rejected_at_construction():
    pytest.importorskip("dlt")

    class FakeClient:
        def __getitem__(self, _name):
            return self

    with pytest.raises(ValueError, match="cursor"):
        mongodb_source(
            database="d",
            collection="c",
            client=FakeClient(),
            projection={"secret": 0},
        )


def test_resource_is_wired_for_merge_with_a_hard_delete_column():
    pytest.importorskip("dlt")

    class FakeClient:
        def __getitem__(self, _name):
            return self

    resource = mongodb_source(database="testdb", collection="testcol", client=FakeClient())

    table = resource.compute_table_schema()
    assert table["write_disposition"] == "merge"
    assert table["columns"]["_deleted"]["hard_delete"] is True
    assert "id" in [name for name, col in table["columns"].items() if col.get("primary_key")]


def test_resource_is_marked_for_the_document_ingestion_path():
    """The marker routes rows through cognify rather than the dlt-row path."""
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    class FakeClient:
        def __getitem__(self, _name):
            return self

    resource = mongodb_source(database="testdb", collection="testcol", client=FakeClient())
    assert getattr(resource, DOCUMENT_SOURCE_ATTR, None) == "mongodb"


# ---------------------------------------------------------------------------
# Against mongomock — an independent implementation of MongoDB query semantics,
# so these do not merely re-confirm the assumptions baked into FakeCollection.
# ---------------------------------------------------------------------------
class _HintTolerantCollection:
    """Drops the ``hint`` kwarg before delegating to mongomock.

    mongomock raises ``OperationFailure: Unrecognized field 'hint'``. The hint only
    steers real server planning, which mongomock does not model, so the adapter
    drops it and keeps mongomock for what it is actually good at here: an
    independent implementation of ``$gt`` / ``$in`` / projection semantics.
    """

    def __init__(self, collection):
        self._collection = collection

    def find(self, query_filter=None, projection=None, **kwargs):
        kwargs.pop("hint", None)
        return self._collection.find(query_filter, projection, **kwargs)


class _Database:
    def __init__(self, database):
        self._database = database

    def __getitem__(self, name):
        return _HintTolerantCollection(self._database[name])


class _Client:
    def __init__(self, client):
        self._client = client

    def __getitem__(self, name):
        return _Database(self._client[name])


class _Mongo(NamedTuple):
    """A mongomock fixture: the hint-tolerant handle for the connector, plus the
    raw collection for the test's own inserts / updates / deletes."""

    client: Any
    collection: Any
    raw: Any


def _mongomock_collection(documents) -> _Mongo:
    mongomock = pytest.importorskip("mongomock")
    raw_client = mongomock.MongoClient()
    raw = raw_client["testdb"]["testcol"]
    if documents:
        raw.insert_many(documents)
    return _Mongo(client=_Client(raw_client), collection=_HintTolerantCollection(raw), raw=raw)


def test_full_cycle_against_mongomock():
    """Backfill, then an edit, an insert and a delete — on real Mongo semantics."""
    mongo = _mongomock_collection(
        [
            {"_id": "a", "updatedAt": 1, "subject": "Alpha"},
            {"_id": "b", "updatedAt": 2, "subject": "Beta"},
        ]
    )
    state = {}

    live, deleted = _run(mongo.collection, state, text_fields=["subject"])
    assert {row["id"] for row in live} == {"a", "b"}
    assert deleted == []
    assert state["last_cursor"] == 2

    # Edit "a", insert "c", delete "b".
    mongo.raw.update_one({"_id": "a"}, {"$set": {"subject": "Alpha v2", "updatedAt": 9}})
    mongo.raw.insert_one({"_id": "c", "updatedAt": 3, "subject": "Gamma"})
    mongo.raw.delete_one({"_id": "b"})

    live, deleted = _run(mongo.collection, state, text_fields=["subject"])

    # "a" changed (cursor), "c" is new (id sweep); "b" is gone.
    assert {row["id"] for row in live} == {"a", "c"}
    assert {row["content"] for row in live} == {"Alpha v2", "Gamma"}
    assert deleted == ["b"]
    assert state["last_cursor"] == 9
    assert state["known_ids"] == ["a", "c"]

    # A third run with nothing changed yields nothing at all.
    live, deleted = _run(mongo.collection, state, text_fields=["subject"])
    assert live == []
    assert deleted == []


def test_projection_is_honored_against_mongomock():
    mongo = _mongomock_collection(
        [{"_id": "a", "updatedAt": 1, "subject": "Keep", "secret": "drop me"}]
    )
    live, _ = _run(
        mongo.collection,
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
    mongo = _mongomock_collection(
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
            client=mongo.client,
            text_fields=["subject"],
        )

    # Sync #1: both documents land in the destination.
    pipeline.run(source(), write_disposition="merge", primary_key="id")
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM mongodb_documents")[0][0] == 2

    # Sync #2: "b" is deleted upstream. The connector emits a hard-delete marker
    # and dlt's merge physically removes the row.
    mongo.raw.delete_one({"_id": "b"})
    pipeline.run(source(), write_disposition="merge", primary_key="id")
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM mongodb_documents")
    assert [row[0] for row in rows] == ["a"]


def test_state_survives_across_pipeline_runs(tmp_path):
    """The high-water mark and id set must persist in dlt resource state."""
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")
    mongo = _mongomock_collection(
        [
            {"_id": "a", "updatedAt": 1, "subject": "Alpha"},
            {"_id": "b", "updatedAt": 2, "subject": "Beta"},
        ]
    )

    pipeline = dlt.pipeline(
        pipeline_name="test_mongodb_state",
        destination=dlt.destinations.duckdb(str(tmp_path / "state.duckdb")),
        dataset_name="mongo",
    )

    def source():
        return mongodb_source(
            database="testdb",
            collection="testcol",
            client=mongo.client,
            text_fields=["subject"],
        )

    pipeline.run(source(), write_disposition="merge", primary_key="id")

    # dlt exposes the committed state as a plain dict; the resource lives under
    # sources[source]["resources"][resource], and the source name dlt picks for a
    # bare resource is an implementation detail, so locate it by content.
    state = pipeline.state
    recorded = next(
        (
            resource_state
            for source in state.get("sources", {}).values()
            for resource_state in source.get("resources", {}).values()
            if "last_cursor" in resource_state
        ),
        None,
    )
    assert recorded is not None, f"no persisted resource state in {state.get('sources')}"
    assert recorded["last_cursor"] == 2
    assert recorded["known_ids"] == ["a", "b"]
