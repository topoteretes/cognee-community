"""Unit tests for the Attio dlt connector.

Everything runs offline against ``FakeAttio``, an in-memory stand-in for Attio's v2 API:

* rendering of attribute values, titles and the row shape,
* listing (pagination, the object / list / notes selection),
* the HTTP client (auth and scope errors, retries, no token in messages),
* a dlt pipeline into a temp sqlite destination covering the sync model: a rerun with
  nothing changed is a no-op, an edit replaces the row, and a record deleted upstream drops out
  of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
from fake_attio import FakeAttio, attribute, name, record, sample_workspace, text

from cognee_community_connector_attio import attio as attio_module
from cognee_community_connector_attio.attio import (
    ATTIO_SOURCE_NAME,
    AttioAccessError,
    AttioAPIError,
    AttioAuthError,
    AttioClient,
    _render_value,
    _retry_after,
    attio_source,
    iter_rows,
)

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"attribute_type": "text", "value": " hello "}, "hello"),
        ({"attribute_type": "number", "value": 42.0}, "42"),
        ({"attribute_type": "number", "value": 4.5}, "4.5"),
        ({"attribute_type": "checkbox", "value": True}, "Yes"),
        ({"attribute_type": "checkbox", "value": False}, "No"),
        ({"attribute_type": "currency", "currency_value": 499, "currency_code": "USD"}, "499 USD"),
        ({"attribute_type": "date", "value": "2026-02-03"}, "2026-02-03"),
        ({"attribute_type": "rating", "value": 4}, "4"),
        ({"attribute_type": "status", "status": {"title": "Won"}}, "Won"),
        ({"attribute_type": "select", "option": {"title": "Aerospace"}}, "Aerospace"),
        ({"attribute_type": "email-address", "email_address": "a@b.test"}, "a@b.test"),
        (
            {
                "attribute_type": "phone-number",
                "original_phone_number": "+1 555 0100",
                "phone_number": "+15550100",
            },
            "+1 555 0100",
        ),
        ({"attribute_type": "domain", "domain": "acme.test"}, "acme.test"),
        (name("Ada", "Test"), "Ada Test"),
        (
            {
                "attribute_type": "location",
                "line_1": "1 Main St",
                "locality": "Springfield",
                "region": "IL",
                "country_code": "US",
            },
            "1 Main St, Springfield, IL, US",
        ),
    ],
)
def test_render_value_covers_attribute_types(value, expected):
    assert _render_value(value, {}) == expected


def test_render_value_resolves_record_references_by_name():
    reference = {
        "attribute_type": "record-reference",
        "target_object": "companies",
        "target_record_id": "c1",
    }
    assert _render_value(reference, {("companies", "c1"): "Acme"}) == "Acme"
    # a record that is not selected (or was deleted) is left out, never shown as a bare id
    assert _render_value(reference, {}) == ""


def test_render_value_skips_actor_references_and_interactions():
    actor = {"attribute_type": "actor-reference", "referenced_actor_id": "member-1"}
    interaction = {"attribute_type": "interaction", "interacted_at": "2026-01-01T00:00:00Z"}
    assert _render_value(actor, {}) == ""
    assert _render_value(interaction, {}) == ""
    assert _render_value({"attribute_type": "something-new"}, {}) == ""


def test_records_render_title_and_fields():
    rows = {row["id"]: row for row in iter_rows(sample_workspace())}

    person = rows["people:p1"]
    assert person["title"] == "Ada Test"
    assert person["url"] == "https://app.attio.com/w/people/p1"
    assert "Type: Person" in person["content"]
    assert "Email addresses: ada@example.test" in person["content"]
    assert "Job title: Engineer" in person["content"]
    # the reference reads as the company's name
    assert "Company: Acme Test Co" in person["content"]
    # the title is not repeated, and actor references and archived attributes stay out
    assert "Name:" not in person["content"]
    assert "m1" not in person["content"]
    assert "hidden" not in person["content"]
    # attio's own bookkeeping attributes are noise for the graph
    assert "Record ID" not in person["content"]
    assert "11111111" not in person["content"]
    assert "Created at" not in person["content"]

    company = rows["companies:c1"]
    assert company["title"] == "Acme Test Co"
    assert "Employees: 42" in company["content"]


def test_custom_objects_are_read_from_the_workspace_schema():
    deal = {row["id"]: row for row in iter_rows(sample_workspace())}["deals:d1"]
    # no "deals" knowledge in the connector: the first text attribute names the record
    assert deal["title"] == "Pilot"
    assert "Value: 499 USD" in deal["content"]
    assert "Stage: Negotiating" in deal["content"]
    assert "Company: Acme Test Co" in deal["content"]


def test_untitled_record_with_values_still_yields_a_row():
    fake = FakeAttio()
    fake.add_object("widgets", "Widget", [attribute("size", "number", "Size")])
    fake.records["widgets"] = [
        record("widgets", "w1", {"size": [{"attribute_type": "number", "value": 3}]}),
        record("widgets", "w2", {}),
    ]
    rows = list(iter_rows(fake))
    assert [row["id"] for row in rows] == ["widgets:w1"]
    assert rows[0]["title"] == "Untitled Widget"


def test_a_record_id_never_becomes_the_title_of_an_unnamed_record():
    fake = FakeAttio()
    fake.add_object(
        "widgets",
        "Widget",
        [attribute("record_id", "text", "Record ID"), attribute("size", "number", "Size")],
    )
    fake.records["widgets"] = [
        record(
            "widgets",
            "w1",
            {
                "record_id": [text("11111111-2222-3333-4444-555555555555")],
                "size": [{"attribute_type": "number", "value": 3}],
            },
        )
    ]

    (row,) = iter_rows(fake)

    assert row["title"] == "Untitled Widget"
    assert row["content"] == "Type: Widget\nSize: 3"


def test_rendering_is_deterministic_across_runs():
    # no timestamps or counters in a row, so an unchanged workspace keeps every content hash
    assert list(iter_rows(sample_workspace())) == list(iter_rows(sample_workspace()))


# ---------------------------------------------------------------------------
# Selection: objects, notes, lists
# ---------------------------------------------------------------------------


def test_notes_become_rows_linked_to_their_record():
    rows = {row["id"]: row for row in iter_rows(sample_workspace())}

    note = rows["note:n1"]
    assert note["title"] == "Intro call"
    assert "Note on Person: Ada Test" in note["content"]
    assert "Ada wants a pilot next quarter." in note["content"]
    # an untitled note falls back to a generic title
    assert rows["note:n2"]["title"] == "Note"
    assert "Note on Company: Acme Test Co" in rows["note:n2"]["content"]


def test_notes_can_be_switched_off():
    rows = list(iter_rows(sample_workspace(), include_notes=False))
    assert not any(row["id"].startswith("note:") for row in rows)


def test_objects_restrict_records_and_notes():
    rows = list(iter_rows(sample_workspace(), objects=["companies"]))
    ids = {row["id"] for row in rows}
    assert ids == {"companies:c1", "note:n2"}


def test_unknown_object_slug_is_rejected_with_the_available_ones():
    with pytest.raises(ValueError, match="deals"):
        list(iter_rows(sample_workspace(), objects=["nope"]))


def test_references_to_unselected_objects_are_left_out():
    rows = {row["id"]: row for row in iter_rows(sample_workspace(), objects=["people"])}
    assert "Company:" not in rows["people:p1"]["content"]


def test_list_entries_are_opt_in():
    assert not any(row["id"].startswith("entry:") for row in iter_rows(sample_workspace()))

    rows = {row["id"]: row for row in iter_rows(sample_workspace(), lists=["pipeline"])}
    entry = rows["entry:pipeline:e1"]
    assert entry["title"] == "Sales pipeline: Ada Test"
    assert "Record: Ada Test" in entry["content"]
    assert "Status: Qualified" in entry["content"]
    assert "Notes: Send pricing" in entry["content"]


def test_unknown_list_slug_is_rejected():
    with pytest.raises(ValueError, match="pipeline"):
        list(iter_rows(sample_workspace(), lists=["nope"]))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _many_people(count):
    fake = FakeAttio()
    fake.add_object("people", "Person", [attribute("name", "personal-name", "Name")])
    fake.records["people"] = [
        record("people", f"p{i}", {"name": [name("Person", str(i))]}) for i in range(count)
    ]
    for i in range(count):
        fake.note(f"n{i}", "people", f"p{i}", f"Note {i}", "body")
    return fake


def test_every_page_of_records_and_notes_is_read(monkeypatch):
    monkeypatch.setattr(attio_module, "RECORDS_PAGE_SIZE", 3)
    monkeypatch.setattr(attio_module, "NOTES_PAGE_SIZE", 2)
    fake = _many_people(7)

    ids = [row["id"] for row in iter_rows(fake)]

    assert sum(i.startswith("people:") for i in ids) == 7
    assert sum(i.startswith("note:") for i in ids) == 7


def test_a_server_that_caps_the_page_size_is_still_read_in_full():
    # asking for 500 and getting 2 must not be taken as the last page
    fake = _many_people(5)
    fake.page_cap = 2

    ids = [row["id"] for row in iter_rows(fake)]

    assert sum(i.startswith("people:") for i in ids) == 5
    assert sum(i.startswith("note:") for i in ids) == 5


def test_a_listing_that_never_ends_fails_instead_of_looping(monkeypatch):
    monkeypatch.setattr(attio_module, "_MAX_PAGES", 3)

    class Stuck(FakeAttio):
        def _page(self, items, query):
            return {"data": items[:1]}  # ignores the offset

    fake = Stuck()
    fake.add_object("people", "Person", [attribute("name", "text", "Name")])
    fake.records["people"] = [record("people", "p1", {"name": [text("Ada")]})]
    with pytest.raises(AttioAPIError):
        list(iter_rows(fake))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _client_with(monkeypatch, responses, sleeps=None):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(attio_module.requests, "request", fake_request)
    sleeps = sleeps if sleeps is not None else []
    return AttioClient("secret-token", sleep=sleeps.append), seen, sleeps


def test_client_sends_the_token_as_a_bearer_header(monkeypatch):
    client, seen, _ = _client_with(monkeypatch, [_Response(200, {"data": []})])

    assert client.request("GET", "/objects") == {"data": []}

    method, url, kwargs = seen[0]
    assert (method, url) == ("GET", "https://api.attio.com/v2/objects")
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_client_retries_rate_limits_and_server_errors(monkeypatch):
    client, seen, sleeps = _client_with(
        monkeypatch,
        [
            _Response(429, headers={"Retry-After": "3"}),
            _Response(503),
            _Response(200, {"data": []}),
        ],
    )

    assert client.request("GET", "/objects") == {"data": []}

    assert len(seen) == 3
    assert sleeps == [3.0, 2.0]  # the header, then backoff


def test_client_gives_up_after_the_retry_budget(monkeypatch):
    client, _, sleeps = _client_with(monkeypatch, [_Response(429)] * attio_module._RETRIES)

    with pytest.raises(AttioAPIError, match="HTTP 429"):
        client.request("GET", "/objects")

    assert len(sleeps) == attio_module._RETRIES - 1


def test_client_retries_network_errors_without_leaking_the_token(monkeypatch):
    boom = attio_module.requests.ConnectionError("failed for Bearer secret-token")
    client, _, _ = _client_with(monkeypatch, [boom] * attio_module._RETRIES)

    with pytest.raises(AttioAPIError) as info:
        client.request("GET", "/objects")

    assert "secret-token" not in str(info.value)
    assert "ConnectionError" in str(info.value)


def test_client_maps_auth_and_scope_errors(monkeypatch):
    client, _, _ = _client_with(monkeypatch, [_Response(401), _Response(403)])

    with pytest.raises(AttioAuthError):
        client.request("GET", "/objects")
    with pytest.raises(AttioAccessError) as info:
        client.request("GET", "/notes")
    assert "/notes" in str(info.value)


def test_client_error_messages_do_not_carry_slugs_or_ids(monkeypatch):
    client, _, _ = _client_with(monkeypatch, [_Response(404)])

    with pytest.raises(AttioAPIError) as info:
        client.request("POST", "/objects/secret_object/records/query")

    assert "secret_object" not in str(info.value)
    assert "/objects/{id}/records/query" in str(info.value)


def test_client_rejects_a_non_object_answer(monkeypatch):
    client, _, _ = _client_with(monkeypatch, [_Response(200, ["not", "an", "object"])])

    with pytest.raises(AttioAPIError, match="unreadable"):
        client.request("GET", "/objects")


@pytest.mark.parametrize("token", ["", "   ", "bad token", "tökén"])
def test_client_rejects_a_malformed_token_without_echoing_it(token):
    with pytest.raises(ValueError) as info:
        AttioClient(token)
    assert token.strip() == "" or token not in str(info.value)


def test_client_repr_redacts_the_token():
    assert "secret-token" not in repr(AttioClient("secret-token"))


def test_retry_after_accepts_seconds_dates_and_falls_back_to_backoff():
    assert _retry_after({"Retry-After": "5"}, 0) == 5.0
    assert _retry_after({"retry-after": "7"}, 0) == 7.0
    assert _retry_after({}, 2) == 4.0
    assert _retry_after({"Retry-After": "soon"}, 1) == 2.0
    assert _retry_after({"Retry-After": "86400"}, 0) == attio_module._MAX_RETRY_WAIT
    # a date in the past means "retry now", not a negative sleep
    assert _retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0) == 0.0


# ---------------------------------------------------------------------------
# Source wiring
# ---------------------------------------------------------------------------


def test_attio_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = attio_source(api_key="test-token")
    assert ATTIO_SOURCE_NAME == "attio"
    assert document_source_tag(source) == "attio"


def test_attio_source_needs_a_token(monkeypatch):
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ATTIO_API_KEY"):
        attio_source()

    monkeypatch.setenv("ATTIO_API_KEY", "from-env")
    assert attio_source() is not None


def test_rows_become_tagged_document_data_items():
    # the row -> document mapping is generic and owned by cognee's ingestion layer
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = next(r for r in iter_rows(sample_workspace()) if r["id"] == "people:p1")
    dlt_row = SimpleNamespace(row_data=row, content_hash="abc123", table_name="attio_records")

    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "attio")

    assert item.system_metadata["source"] == "attio"
    assert item.system_metadata["external_id"] == "people:p1"
    assert item.data.startswith("# Ada Test")
    assert "Job title: Engineer" in item.data


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "attio.db").as_posix()
    return dlt.pipeline(
        pipeline_name="attio_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="attio_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _sync(dlt, tmp_path, fake, **kwargs):
    pipeline = _pipeline(dlt, tmp_path)
    pipeline.run(attio_source(client=fake, **kwargs))
    return pipeline


def _read_rows(pipeline):
    """Return {id: row-dict} for the attio_records table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM attio_records") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_records_and_notes(dlt_mod, tmp_path):
    rows = _read_rows(_sync(dlt_mod, tmp_path, sample_workspace()))

    assert set(rows) == {
        "people:p1",
        "people:p2",
        "companies:c1",
        "deals:d1",
        "note:n1",
        "note:n2",
    }
    assert "Engineer" in rows["people:p1"]["content"]


def test_rerun_with_nothing_changed_is_a_no_op(dlt_mod, tmp_path):
    first = _read_rows(_sync(dlt_mod, tmp_path, sample_workspace()))
    second = _read_rows(_sync(dlt_mod, tmp_path, sample_workspace()))

    assert first == second


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _sync(dlt_mod, tmp_path, sample_workspace())

    edited = sample_workspace()
    edited.records["people"][0]["values"]["job_title"] = [text("Staff Engineer")]
    rows = _read_rows(_sync(dlt_mod, tmp_path, edited))

    assert "Staff Engineer" in rows["people:p1"]["content"]
    assert "Job title: Engineer" not in rows["people:p1"]["content"]


def test_deleted_record_and_note_are_removed_on_resync(dlt_mod, tmp_path):
    # A record deleted in Attio simply stops being listed. It must fall out of the replace
    # load so cognee's orphan cleanup forgets it; a merge cursor could never see it.
    _sync(dlt_mod, tmp_path, sample_workspace())

    after = sample_workspace()
    after.records["people"] = [r for r in after.records["people"] if r["id"]["record_id"] != "p2"]
    after.notes = [n for n in after.notes if n["id"]["note_id"] != "n1"]
    rows = _read_rows(_sync(dlt_mod, tmp_path, after))

    assert "people:p2" not in rows
    assert "note:n1" not in rows
    assert {"people:p1", "companies:c1", "note:n2"} <= set(rows)


def test_a_failed_run_leaves_the_previous_snapshot_untouched(dlt_mod, tmp_path):
    # Under replace a partial snapshot would be reconciled as mass deletion, so any API
    # failure must abort the run instead of committing what was read so far.
    pipeline = _sync(dlt_mod, tmp_path, sample_workspace())
    before = _read_rows(pipeline)

    broken = sample_workspace()
    broken.fail_on = "/notes"
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _pipeline(dlt_mod, tmp_path).run(attio_source(client=broken))

    assert _read_rows(_pipeline(dlt_mod, tmp_path)) == before
