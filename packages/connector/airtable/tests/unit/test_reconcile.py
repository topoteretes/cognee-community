from __future__ import annotations

import copy
from datetime import datetime

import pytest
import requests

from cognee_community_connector_airtable import airtable as connector
from cognee_community_connector_airtable.airtable import AirtableError, _reconcile
from tests.fakes import FakeAirtableSession, ScriptedSession, comment, record, response, table

BASE = "appOne"
TABLE = "tblOne"
RECORD_ID = f"{BASE}/{TABLE}/record/recOne"
SCHEMA_ID = f"{BASE}/{TABLE}/schema"


def sync(session, state=None, **kwargs):
    return _reconcile(session, BASE, {} if state is None else state, **kwargs)


def by_id(rows):
    return {row["id"]: row for row in rows}


def live_rows(rows):
    return [row for row in rows if not row.get("_deleted")]


def deleted_ids(rows):
    return {row["id"] for row in rows if row.get("_deleted")}


def test_initial_sync_renders_records_comments_and_independent_schema():
    session = FakeAirtableSession(comments={(TABLE, "recOne"): [comment()]})
    original = {"tables": {}}
    rows, state = sync(session, original)
    documents = by_id(rows)

    assert set(documents) == {RECORD_ID, SCHEMA_ID}
    assert "Cedar ships violet teapots." in documents[RECORD_ID]["content"]
    assert "Delivery uses amber crates." in documents[RECORD_ID]["content"]
    assert "Notes" in documents[RECORD_ID]["content"]
    assert "Customers" in documents[SCHEMA_ID]["content"]
    assert "multilineText" in documents[SCHEMA_ID]["content"]
    assert original == {"tables": {}}
    assert set(state["tables"][TABLE]["documents"]) == set(documents)
    assert all(set(row) <= {"id", "title", "content", "url", "_deleted"} for row in rows)
    assert all(row["url"].startswith("https://airtable.com/") for row in rows)
    record_call = next(call for call in session.calls if call["path"] == "/v0/appOne/tblOne")
    assert str(record_call["params"]["returnFieldsByFieldId"]).lower() == "true"
    assert all(call["timeout"] == 30 for call in session.calls)


def test_complete_pagination_for_records_and_comments():
    session = FakeAirtableSession(
        records={TABLE: [record(), record("recTwo", "Birch uses silver ladders.")]},
        comments={(TABLE, "recOne"): [comment(), comment("comTwo", "Cargo travels Tuesday.")]},
        page_size=1,
    )
    rows, state = sync(session)
    assert set(by_id(rows)) == {RECORD_ID, f"{BASE}/{TABLE}/record/recTwo", SCHEMA_ID}
    assert "Cargo travels Tuesday." in by_id(rows)[RECORD_ID]["content"]
    calls = [call for call in session.calls if call["path"] == "/v0/appOne/tblOne"]
    assert len(calls) == 2
    assert calls[1]["params"]["offset"] == "1"
    calls = [call for call in session.calls if call["path"].endswith("recOne/comments")]
    assert len(calls) == 2
    assert calls[1]["params"]["offset"] == "1"
    assert len(state["tables"][TABLE]["documents"]) == 3


def test_unchanged_inventory_emits_nothing_but_still_reconciles_comments():
    session = FakeAirtableSession(comments={(TABLE, "recOne"): [comment()]})
    _, state = sync(session)
    session.calls.clear()
    rows, next_state = sync(session, state)
    assert rows == []
    assert next_state == state
    assert any(call["path"].endswith("/comments") for call in session.calls)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-01T10:00:00.000Z",
        "2026-09-01T10:00:00.000Z",
        "",
        None,
    ],
)
def test_content_edits_are_not_gated_by_equal_older_or_blank_timestamps(timestamp):
    session = FakeAirtableSession()
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.records[TABLE][0] = record(text="Cedar now ships copper kites.", timestamp=timestamp)
    rows, next_state = sync(session, state)
    assert list(by_id(rows)) == [RECORD_ID]
    assert "copper kites" in rows[0]["content"]
    assert "violet teapots" not in rows[0]["content"]
    assert next_state["tables"][TABLE]["watermark"] == prior["tables"][TABLE]["watermark"]
    assert state == prior


def test_cursor_only_change_advances_watermark_without_document_version():
    session = FakeAirtableSession()
    _, state = sync(session)
    session.records[TABLE][0]["fields"]["fldModified"] = "2026-10-02T10:00:00.000Z"
    rows, next_state = sync(session, state)
    assert rows == []
    assert next_state["tables"][TABLE]["documents"] == state["tables"][TABLE]["documents"]
    watermark = next_state["tables"][TABLE]["watermark"]
    assert datetime.fromisoformat(watermark.replace("Z", "+00:00")).day == 2


def test_new_and_restored_ids_ingest_even_when_their_timestamp_is_old():
    session = FakeAirtableSession()
    _, state = sync(session)
    session.records[TABLE].append(record("recTwo", timestamp="2020-01-01T00:00:00.000Z"))
    rows, state = sync(session, state)
    assert set(by_id(rows)) == {f"{BASE}/{TABLE}/record/recTwo"}
    session.records[TABLE] = []
    rows, state = sync(session, state)
    assert deleted_ids(rows) == {RECORD_ID, f"{BASE}/{TABLE}/record/recTwo"}
    session.records[TABLE] = [record(timestamp="2020-01-01T00:00:00.000Z")]
    rows, _ = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID}
    assert not rows[0].get("_deleted")


def test_computed_field_change_is_compared_without_cursor_change():
    session = FakeAirtableSession()
    session.schema[0]["fields"].append(
        {"id": "fldFormula", "name": "Forecast", "type": "formula", "options": {"formula": "1+1"}}
    )
    session.records[TABLE][0]["fields"]["fldFormula"] = "Golden harbor"
    _, state = sync(session)
    session.records[TABLE][0]["fields"]["fldFormula"] = "Emerald harbor"
    rows, _ = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID}
    assert "Emerald harbor" in rows[0]["content"]
    assert "Golden harbor" not in rows[0]["content"]


def test_comment_only_edits_and_deletes_replace_record_content():
    session = FakeAirtableSession(comments={(TABLE, "recOne"): [comment()]})
    _, state = sync(session)
    session.comments[TABLE, "recOne"][0]["text"] = "Delivery uses emerald crates."
    rows, state = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID}
    assert "emerald crates" in rows[0]["content"]
    assert "amber crates" not in rows[0]["content"]
    session.comments[TABLE, "recOne"] = []
    rows, _ = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID}
    assert "emerald crates" not in rows[0]["content"]


def test_table_and_field_rename_keep_ids_and_replace_rendered_names():
    session = FakeAirtableSession()
    _, state = sync(session)
    session.schema[0]["name"] = "Suppliers"
    session.schema[0]["fields"][0]["name"] = "Shipping facts"
    rows, _ = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID, SCHEMA_ID}
    assert "Shipping facts" in by_id(rows)[RECORD_ID]["content"]
    assert "Suppliers" in by_id(rows)[SCHEMA_ID]["content"]
    assert '"name":"Customers"' not in by_id(rows)[SCHEMA_ID]["content"]


def test_source_order_does_not_change_staged_documents_or_hashes():
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record(), record("recTwo")], "tblTwo": [record("recThree")]},
        comments={(TABLE, "recOne"): [comment(), comment("comTwo", "Another fact.")]},
    )
    _, state = sync(session)
    session.schema.reverse()
    for schema in session.schema:
        schema["fields"].reverse()
    session.records[TABLE].reverse()
    for item in session.records[TABLE]:
        item["fields"] = dict(reversed(list(item["fields"].items())))
    session.comments[TABLE, "recOne"].reverse()
    rows, next_state = sync(session, state)
    assert rows == []
    assert next_state == state


def test_attachment_urls_and_thumbnails_never_enter_staged_rows():
    session = FakeAirtableSession()
    session.schema[0]["fields"].append(
        {"id": "fldAttachment", "name": "Report", "type": "multipleAttachments"}
    )
    attachment = {
        "id": "attOne",
        "filename": "cargo.pdf",
        "type": "application/pdf",
        "size": 42,
        "url": "https://temporary.example.test/URL_A",
        "thumbnails": {"small": {"url": "https://temporary.example.test/THUMB_A"}},
    }
    session.records[TABLE][0]["fields"]["fldAttachment"] = [attachment]
    first, state = sync(session)
    document = by_id(first)[RECORD_ID]
    assert "cargo.pdf" in document["content"]
    assert "attOne" in document["content"]
    assert "URL_A" not in str(document)
    assert "THUMB_A" not in str(document)
    attachment["url"] = "https://temporary.example.test/URL_B"
    attachment["thumbnails"]["small"]["url"] = "https://temporary.example.test/THUMB_B"
    rows, next_state = sync(session, state)
    assert rows == []
    assert next_state == state
    attachment["filename"] = "cargo-revised.pdf"
    rows, _ = sync(session, state)
    assert set(by_id(rows)) == {RECORD_ID}
    assert "cargo-revised.pdf" in rows[0]["content"]


def test_empty_table_deletes_every_record_but_retains_schema():
    session = FakeAirtableSession(records={TABLE: [record(), record("recTwo")]})
    _, state = sync(session)
    session.records[TABLE] = []
    rows, next_state = sync(session, state)
    assert deleted_ids(rows) == {RECORD_ID, f"{BASE}/{TABLE}/record/recTwo"}
    assert set(next_state["tables"][TABLE]["documents"]) == {SCHEMA_ID}
    assert sync(session, next_state)[0] == []


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_deleted_table_removes_schema_and_all_known_records(explicit_selection):
    session = FakeAirtableSession(records={TABLE: [record(), record("recTwo")]})
    kwargs = {"table_ids": [TABLE]} if explicit_selection else {}
    _, state = sync(session, **kwargs)
    session.schema = []
    rows, next_state = sync(session, state, **kwargs)
    assert deleted_ids(rows) == {RECORD_ID, f"{BASE}/{TABLE}/record/recTwo", SCHEMA_ID}
    assert not next_state["tables"].get(TABLE, {}).get("documents")


def test_deselected_table_is_preserved_even_when_absent_from_current_schema():
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record()], "tblTwo": [record("recTwo")]},
    )
    _, state = sync(session)
    prior = copy.deepcopy(state["tables"]["tblTwo"])
    session.schema = [session.schema[0]]
    rows, next_state = sync(session, state, table_ids=[TABLE])
    assert rows == []
    assert next_state["tables"]["tblTwo"] == prior
    assert not any("tblTwo" in call["path"] for call in session.calls[-3:])


def test_unknown_table_on_first_use_fails_configuration():
    with pytest.raises(ValueError):
        sync(FakeAirtableSession(), table_ids=["tblUnknown"])


def test_schema_flag_does_not_skip_mandatory_metadata_and_removes_previous_schema():
    session = FakeAirtableSession()
    _, state = sync(session)
    session.calls.clear()
    rows, next_state = sync(session, state, include_schema=False)
    assert deleted_ids(rows) == {SCHEMA_ID}
    assert next_state["tables"][TABLE]["documents"].keys() == {RECORD_ID}
    assert session.calls[0]["path"] == "/v0/meta/bases/appOne/tables"


def test_comments_flag_skips_requests_and_removes_previous_comment_content():
    session = FakeAirtableSession(comments={(TABLE, "recOne"): [comment()]})
    _, state = sync(session)
    session.calls.clear()
    rows, _ = sync(session, state, include_comments=False)
    assert set(by_id(rows)) == {RECORD_ID}
    assert "amber crates" not in rows[0]["content"]
    assert not any(call["path"].endswith("/comments") for call in session.calls)


def test_per_table_cursor_mapping():
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record()], "tblTwo": [record("recTwo")]},
    )
    session.schema[1]["fields"][1]["name"] = "Updated at"
    rows, state = sync(
        session, last_modified_field={TABLE: "Last modified time", "tblTwo": "Updated at"}
    )
    assert len(rows) == 4
    assert set(state["tables"]) == {TABLE, "tblTwo"}


@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "invalid", "date_only"])
def test_cursor_configuration_is_validated_even_without_schema_documents(mutation):
    session = FakeAirtableSession()
    field = session.schema[0]["fields"][1]
    if mutation == "missing":
        session.schema[0]["fields"].pop()
    elif mutation == "wrong_type":
        field["type"] = "singleLineText"
    elif mutation == "invalid":
        field["options"]["isValid"] = False
    else:
        field["options"]["result"]["type"] = "date"
    with pytest.raises(ValueError):
        sync(session, include_schema=False)


def test_failed_later_comments_request_does_not_mutate_existing_state():
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record()], "tblTwo": [record("recTwo")]},
    )
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.records[TABLE][0]["fields"]["fldText"] = "A pending unpublished update."
    session.failures["/v0/appOne/tblTwo/recTwo/comments"] = [response({}, 403)]
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"tables": None},
        {"tables": {}},
        {"tables": [{}]},
        {"tables": [table(), table()]},
    ],
)
def test_malformed_schema_is_not_an_empty_success(payload):
    state = {"tables": {TABLE: {"watermark": None, "documents": {RECORD_ID: "prior"}}}}
    prior = copy.deepcopy(state)
    with pytest.raises((AirtableError, ValueError)):
        sync(ScriptedSession([response(payload)]), state)
    assert state == prior


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"records": None},
        {"records": {}},
        {"records": [{}]},
        {"records": [{"id": "recOne", "fields": None}]},
        {"records": [record(), record()]},
        {"records": [], "offset": 7},
    ],
)
def test_malformed_record_inventory_preserves_existing_state(payload):
    session = FakeAirtableSession()
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.failures["/v0/appOne/tblOne"] = [response(payload)]
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"comments": None}, {"comments": [{}]}, {"comments": [comment(), comment()]}],
)
def test_malformed_comments_cannot_silently_remove_previous_comments(payload):
    session = FakeAirtableSession(comments={(TABLE, "recOne"): [comment()]})
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.failures["/v0/appOne/tblOne/recOne/comments"] = [response(payload)]
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


@pytest.mark.parametrize("kind", ["records", "comments"])
def test_repeated_pagination_offset_fails_instead_of_publishing_partial_inventory(kind):
    session = FakeAirtableSession()
    _, state = sync(session)
    prior = copy.deepcopy(state)
    items = [record()] if kind == "records" else [comment()]
    path = "/v0/appOne/tblOne" if kind == "records" else "/v0/appOne/tblOne/recOne/comments"
    session.failures[path] = [
        response({kind: items, "offset": "repeat"}),
        response({kind: [], "offset": "repeat"}),
    ]
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_failures_do_not_retry_or_change_state(status):
    session = ScriptedSession([response({"error": {"type": "PERMISSION_DENIED"}}, status)])
    state = {"tables": {}}
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert len(session.calls) == 1
    assert state == {"tables": {}}


@pytest.mark.parametrize("retry_after, minimum_wait", [(None, 30), ("1", 30), ("45", 45)])
def test_rate_limit_retry_honors_airtable_cooldown(retry_after, minimum_wait, fast_http):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    session = FakeAirtableSession(
        failures={"/v0/meta/bases/appOne/tables": [response({}, 429, headers=headers)]}
    )
    rows, _ = sync(session)
    assert len(rows) == 2
    assert any(wait >= minimum_wait for wait in fast_http)


def test_transport_and_server_failures_retry_then_use_complete_response():
    session = FakeAirtableSession(
        failures={"/v0/meta/bases/appOne/tables": [requests.Timeout("timeout"), response({}, 503)]}
    )
    rows, _ = sync(session)
    assert len(rows) == 2
    schema_calls = [call for call in session.calls if "/meta/" in call["path"]]
    assert len(schema_calls) == 3


@pytest.mark.parametrize(
    "failure", [requests.ConnectionError("offline"), response({}, 503), response({}, 429)]
)
def test_retry_exhaustion_is_bounded_and_preserves_state(failure):
    session = ScriptedSession([failure] * 5)
    state = {"tables": {}}
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert len(session.calls) == 5
    assert state == {"tables": {}}


def test_invalid_json_is_not_treated_as_an_empty_schema():
    reply = response({})
    reply._content = b"not-json"
    with pytest.raises((AirtableError, ValueError)):
        sync(ScriptedSession([reply]))


def test_request_pacing_applies_across_schema_records_and_comments(monkeypatch):
    clock = [100.0]
    starts = []
    session = FakeAirtableSession()
    original_get = session.get

    def get(*args, **kwargs):
        starts.append(clock[0])
        return original_get(*args, **kwargs)

    monkeypatch.setattr(connector, "_REQUEST_INTERVAL", 0.21)
    monkeypatch.setattr(connector.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        connector.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration)
    )
    monkeypatch.setattr(session, "get", get)
    sync(session)
    assert len(starts) == 3
    assert all(later - earlier >= 0.209 for earlier, later in zip(starts, starts[1:], strict=False))


@pytest.mark.parametrize("value", ["not-a-timestamp", "2026-10-01", 17, [], {}])
def test_malformed_cursor_values_do_not_publish_partial_inventory(value):
    session = FakeAirtableSession()
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.records[TABLE][0]["fields"]["fldModified"] = value
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


def test_unknown_field_id_aborts_incomplete_schema_resolution():
    session = FakeAirtableSession()
    _, state = sync(session)
    prior = copy.deepcopy(state)
    session.records[TABLE][0]["fields"]["fldUnknown"] = "New unsupported field."
    with pytest.raises((AirtableError, ValueError)):
        sync(session, state)
    assert state == prior


def test_watermarks_normalize_timezones_before_comparison():
    session = FakeAirtableSession(records={TABLE: [record(timestamp="2026-10-01T15:30:00+05:30")]})
    _, state = sync(session)
    initial = state["tables"][TABLE]["watermark"]
    assert datetime.fromisoformat(initial).hour == 10
    session.records[TABLE][0]["fields"]["fldModified"] = "2026-10-01T06:00:00-05:00"
    rows, state = sync(session, state)
    assert rows == []
    assert datetime.fromisoformat(state["tables"][TABLE]["watermark"]).hour == 11


def test_upstream_errors_never_disclose_request_credentials(caplog):
    secret = "secret-private-airtable-pat"
    session = ScriptedSession([requests.Timeout(secret)] * 5)
    session.headers["Authorization"] = f"Bearer {secret}"
    with pytest.raises((AirtableError, ValueError)) as failure:
        sync(session)
    assert secret not in str(failure.value)
    assert secret not in caplog.text


def test_missing_comments_offset_aborts_without_changing_state():
    session = FakeAirtableSession()
    session.failures["/v0/appOne/tblOne/recOne/comments"] = [response({"comments": []})]
    state = {}
    with pytest.raises(connector.AirtableError, match="pagination offset"):
        connector._reconcile(session, "appOne", state)
    assert state == {}
