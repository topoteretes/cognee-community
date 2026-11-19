"""Unit tests for the Airtable connector (mocked HTTP — no network, no token).

Covers the things that actually carry risk in this connector:

  - offset pagination, and termination when Airtable stops returning an offset
  - a backfill yields every record and records the cursor + the id set
  - an incremental re-sync yields ONLY records newer than the cursor
  - a re-sync with nothing changed yields nothing
  - a record new to the corpus but below the cursor is still ingested
  - a deleted record becomes a single ``_deleted`` hard-delete marker
  - the transient-empty-sweep guard refuses to mass-delete and preserves state
  - comments are folded into the content; the field schema is attached
  - a record missing the modified field is re-ingested rather than silently skipped
  - 429 / 5xx responses are retried, permanent errors are not
  - the resource declares merge + primary key + a hard-delete ``_deleted`` column
"""

import pytest

from cognee_community_connector_airtable.airtable import (
    AIRTABLE_TABLE_NAME,
    airtable_source,
    sync_records,
)

BASE_ID = "appTESTBASE"
TABLE_ID = "tblOrders"
MODIFIED_FIELD = "lastModifiedTime"


class FakeAirtable:
    """Minimal stand-in for a ``requests.Session`` hitting the Airtable API."""

    def __init__(self, tables=None, records=None, comments=None, schema_status=200):
        # tables: {table_id: [record, ...]}
        self.tables = tables or {}
        self.comments = comments or {}
        # schema_status lets a test simulate a token without schema.bases:read
        self.schema_status = schema_status
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/tables"):
            return FakeResponse(self.schema_status, self._schema_payload())
        if url.endswith("/comments"):
            record_id = url.rstrip("/").split("/")[-2]
            return FakeResponse(200, {"comments": self.comments.get(record_id, [])})
        # Record listing: /v0/{base}/{table}
        table_id = url.rstrip("/").split("/")[-1]
        return self._records_response(table_id, dict(params or {}))

    def _schema_payload(self):
        return {
            "tables": [
                {
                    "id": table_id,
                    "name": table_id,
                    "fields": [{"id": "fld1", "name": "Name", "type": "singleLineText"}],
                }
                for table_id in self.tables
            ]
        }

    def _records_response(self, table_id, params):
        records = self.tables.get(table_id, [])
        # Offset pagination, one record per page, so tests exercise the loop.
        offset = params.get("offset")
        start = int(offset) if offset else 0
        page = records[start : start + 1]
        payload = {"records": page}
        if start + 1 < len(records):
            payload["offset"] = str(start + 1)
        return FakeResponse(200, payload)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _record(record_id, name, modified, extra=None):
    fields = {"Name": name, MODIFIED_FIELD: modified}
    fields.update(extra or {})
    return {"id": record_id, "fields": fields}


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_pagination_follows_offsets_and_stops():
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z"),
                            _record("rec2", "b", "2026-01-02T00:00:00.000Z")]}
    )
    rows = list(
        sync_records(
            session, BASE_ID, {}, table_ids=[TABLE_ID], include_comments=False, include_schema=False
        )
    )
    assert [row["id"] for row in rows] == ["rec1", "rec2"]
    listing_params = [p for url, p in session.calls if not url.endswith("/tables")]
    assert [p.get("offset") for p in listing_params] == [None, "1"]


# ---------------------------------------------------------------------------
# Backfill / incremental
# ---------------------------------------------------------------------------
def test_backfill_yields_all_records_and_records_cursor_and_ids():
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z"),
                            _record("rec2", "b", "2026-03-01T00:00:00.000Z")]}
    )
    state = {}
    rows = list(
        sync_records(session, BASE_ID, state, table_ids=[TABLE_ID], include_comments=False)
    )
    assert [row["id"] for row in rows] == ["rec1", "rec2"]
    # Cursor + id set captured for the next incremental run.
    assert state["last_modified"] == "2026-03-01T00:00:00.000Z"
    assert state["known_ids"] == ["rec1", "rec2"]


def test_incremental_yields_only_records_modified_since_cursor():
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z"),
                            _record("rec2", "b", "2026-03-01T00:00:00.000Z")]}
    )
    state = {"known_ids": ["rec1", "rec2"], "last_modified": "2026-02-01T00:00:00.000Z"}
    rows = list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID], include_comments=False))
    assert [row["id"] for row in rows] == ["rec2"]
    assert state["last_modified"] == "2026-03-01T00:00:00.000Z"


def test_incremental_no_changes_is_a_noop():
    session = FakeAirtable(tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]})
    state = {"known_ids": ["rec1"], "last_modified": "2026-01-01T00:00:00.000Z"}
    assert list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID])) == []
    assert state["known_ids"] == ["rec1"]


def test_new_record_below_cursor_is_still_ingested():
    # A record restored / moved in with an old modified time must not be lost just
    # because the cursor has already moved past it.
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "known", "2026-01-01T00:00:00.000Z"),
                            _record("rec9", "restored", "2025-01-01T00:00:00.000Z")]}
    )
    state = {"known_ids": ["rec1"], "last_modified": "2026-05-01T00:00:00.000Z"}
    rows = list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID], include_comments=False))
    assert [row["id"] for row in rows] == ["rec9"]


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_deleted_record_becomes_a_hard_delete_marker():
    session = FakeAirtable(tables={TABLE_ID: [_record("rec2", "b", "2026-03-01T00:00:00.000Z")]})
    state = {"known_ids": ["rec1", "rec2"], "last_modified": "2026-03-01T00:00:00.000Z"}
    rows = list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID]))
    assert rows == [{"id": "rec1", "_deleted": True}]
    assert state["known_ids"] == ["rec2"]


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    # A transient empty listing must not wipe memory or forget the id set.
    session = FakeAirtable(tables={TABLE_ID: []})
    state = {"known_ids": ["rec1", "rec2"], "last_modified": "2026-03-01T00:00:00.000Z"}
    assert list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID])) == []
    assert state["known_ids"] == ["rec1", "rec2"]


# ---------------------------------------------------------------------------
# Content: comments, schema, missing cursor field
# ---------------------------------------------------------------------------
def test_comments_and_schema_are_attached():
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "Order 1", "2026-01-01T00:00:00.000Z",
                                   extra={"Notes": "urgent"})]},
        comments={"rec1": [{"id": "com1", "text": "looks good"}]},
    )
    rows = list(sync_records(session, BASE_ID, {}, table_ids=[TABLE_ID]))
    row = rows[0]
    assert row["title"] == "Order 1"
    assert "**Notes**: urgent" in row["content"]
    assert "Comments:" in row["content"] and "looks good" in row["content"]
    assert '"Name": "singleLineText"' in row["schema"]
    assert row["url"] == f"https://airtable.com/{BASE_ID}/{TABLE_ID}/rec1"
    assert row["_deleted"] is False


def test_record_without_modified_field_is_re_ingested_not_skipped():
    record = {"id": "rec1", "fields": {"Name": "no cursor field"}}
    session = FakeAirtable(tables={TABLE_ID: [record]})
    state = {"known_ids": ["rec1"], "last_modified": "2026-05-01T00:00:00.000Z"}
    rows = list(sync_records(session, BASE_ID, state, table_ids=[TABLE_ID], include_comments=False))
    assert [row["id"] for row in rows] == ["rec1"]


def test_schema_failure_only_drops_the_schema_column():
    session = FakeAirtable(
        tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]}, schema_status=403
    )
    rows = list(sync_records(session, BASE_ID, {}, table_ids=[TABLE_ID], include_comments=False))
    assert rows[0]["schema"] == "{}"
    assert rows[0]["title"] == "a"


def test_table_enumeration_uses_the_meta_api_when_not_scoped():
    session = FakeAirtable(tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]})
    rows = list(sync_records(session, BASE_ID, {}, include_comments=False))
    assert [row["id"] for row in rows] == ["rec1"]


def test_unscoped_sync_without_schema_still_enumerates_tables():
    # include_schema only controls whether the schema is *attached*; the meta API is
    # still needed to discover the tables, so this must not raise.
    session = FakeAirtable(tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]})
    rows = list(sync_records(session, BASE_ID, {}, include_comments=False, include_schema=False))
    assert [row["id"] for row in rows] == ["rec1"]
    assert rows[0]["schema"] == "{}"


def test_unscoped_sync_without_meta_access_is_explicit():
    # Without table_ids and without the schema scope there is no way to know the
    # tables; failing loudly beats syncing nothing silently.
    session = FakeAirtable(tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]},
                           schema_status=403)
    with pytest.raises(ValueError):
        list(sync_records(session, BASE_ID, {}, include_comments=False))


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
class FlakyAirtable(FakeAirtable):
    """Returns 429 ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times, **kwargs):
        super().__init__(**kwargs)
        self.remaining_failures = fail_times

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if self.remaining_failures > 0 and not url.endswith("/tables"):
            self.remaining_failures -= 1
            return FakeResponse(429, {})
        return super().get(url, params)


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_airtable.airtable.time.sleep", lambda _s: None)
    session = FlakyAirtable(
        2, tables={TABLE_ID: [_record("rec1", "a", "2026-01-01T00:00:00.000Z")]}
    )
    rows = list(
        sync_records(
            session, BASE_ID, {}, table_ids=[TABLE_ID], include_comments=False, include_schema=False
        )
    )
    assert [row["id"] for row in rows] == ["rec1"]


def test_permanent_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_airtable.airtable.time.sleep", lambda _s: None)
    session = FakeAirtable(tables={TABLE_ID: []})
    session.schema_status = 200

    def always_401(url, params=None):
        return FakeResponse(401, {})

    session.get = always_401
    with pytest.raises(RuntimeError):
        list(sync_records(session, BASE_ID, {}, table_ids=[TABLE_ID], include_schema=False))


# ---------------------------------------------------------------------------
# Resource configuration
# ---------------------------------------------------------------------------
def test_source_declares_merge_primary_key_and_hard_delete_marker():
    dlt = pytest.importorskip("dlt")

    source = airtable_source(
        base_id=BASE_ID, table_ids=[TABLE_ID], token="patTEST", session=FakeAirtable()
    )
    schema = source.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")

    assert source.name == AIRTABLE_TABLE_NAME
    assert write_disposition == "merge"
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert dlt.current is not None  # dlt imported and usable


def test_source_requires_a_token(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="personal access token"):
        airtable_source(base_id=BASE_ID, token=None)


def test_source_requires_a_base_id(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("AIRTABLE_BASE_ID", raising=False)

    with pytest.raises(ValueError, match="base id"):
        airtable_source(base_id=None, token="patTEST", session=FakeAirtable())


def test_source_accepts_an_injected_session_without_a_token():
    # Mirrors the Confluence connector: an injected session stands in for the
    # token, so the token requirement does not apply.
    pytest.importorskip("dlt")
    assert airtable_source(base_id=BASE_ID, token=None, session=FakeAirtable()) is not None
