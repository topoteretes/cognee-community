"""Unit tests for the Zoho CRM dlt connector (no credentials or network needed).

The fake API mirrors behaviour observed on a live Zoho CRM v8 account:
``fields`` is mandatory and capped at 50 (``LIMIT_EXCEEDED``), ``If-Modified-Since``
is inclusive and answers ``304`` when nothing changed, an empty deleted-records
list answers ``204``, paging uses ``next_page_token``, and a wrong data centre
answers ``200 {"error": "invalid_client"}`` from the token endpoint.
"""

from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import NAMESPACE_OID, uuid5

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_zoho_crm import zoho_crm as zc
from cognee_community_connector_zoho_crm.zoho_crm import (
    ATTACHMENTS_TABLE,
    NOTES_TABLE,
    RECORDS_TABLE,
    ZOHO_SOURCE_NAME,
    _module_fields,
    _record_row,
    _render_value,
    zoho_crm_source,
)

API = "https://www.zohoapis.eu"

FIELD_META = {
    "Leads": [
        ("Owner", "ownerlookup", "Lead Owner"),
        ("Company", "text", "Company"),
        ("Full_Name", "text", "Lead Name"),
        ("Email", "email", "Email"),
        ("Phone", "phone", "Phone"),
        ("Lead_Status", "picklist", "Lead Status"),
        ("Modified_Time", "datetime", "Modified Time"),
        ("Last_Activity_Time", "datetime", "Last Activity Time"),
        ("Record_Image", "profileimage", "Lead Image"),
        ("Description", "textarea", "Description"),
        ("id", "bigint", "ID"),
    ],
    "Deals": [
        ("Deal_Name", "text", "Deal Name"),
        ("Amount", "currency", "Amount"),
        ("Stage", "picklist", "Stage"),
        ("Account_Name", "lookup", "Account Name"),
        ("Sales_Cycle_Duration", "integer", "Sales Cycle Duration"),
        ("id", "bigint", "ID"),
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            raise httpx.HTTPStatusError("error", request=request, response=self)


def _lead(rid, name="Ann Lee", modified="2026-01-01T00:00:00+00:00", **kw):
    lead = {
        "id": rid,
        "Owner": {"name": "Sam Rep", "id": "u1", "email": "rep@example.com"},
        "Company": "Acme",
        "Full_Name": name,
        "Email": "ann@example.com",
        "Phone": "+1 555 0100",
        "Lead_Status": "Contacted",
        "Modified_Time": modified,
        "Last_Activity_Time": modified,
        "Description": None,
    }
    lead.update(kw)
    return lead


def _note(nid, parent_id, module="Leads", modified="2026-01-01T00:00:00+00:00", **kw):
    note = {
        "id": nid,
        "Note_Title": "Call",
        "Note_Content": f"Discussed pricing ({nid})",
        "Parent_Id": {"module": {"api_name": module}, "name": "Ann Lee", "id": parent_id},
        "Owner": {"name": "Sam Rep", "email": "rep@example.com"},
        "Created_Time": "2026-01-01T00:00:00+00:00",
        "Modified_Time": modified,
    }
    note.update(kw)
    return note


class FakeZoho:
    """In-memory Zoho CRM v8 (records, notes, attachments, deleted feeds)."""

    def __init__(self, records=None, notes=(), attachments=(), page_size=2):
        self.records = {m: list(v) for m, v in (records or {}).items()}
        self.notes = list(notes)
        self.attachments = list(attachments)  # dicts incl. "_bytes"
        self.deleted = {}  # module -> [{"id", "deleted_time"}]
        self.page_size = page_size
        self.calls = []
        self.token_posts = []
        self.valid_token = None
        self.region_ok = "https://accounts.zoho.eu"
        self.fail_path = None

    def post(self, url, params=None):
        self.token_posts.append(url)
        if not url.startswith(self.region_ok):
            return FakeResponse(200, {"error": "invalid_client"})
        assert params["grant_type"] == "refresh_token"
        self.valid_token = f"tok{len(self.token_posts)}"
        return FakeResponse(200, {"access_token": self.valid_token, "api_domain": API})

    def get(self, url, params=None, headers=None):
        params = dict(params or {})
        headers = headers or {}
        path = urlsplit(url).path.split(f"/crm/{zc.API_VERSION}/", 1)[1]
        self.calls.append((path, params, headers))
        if headers.get("Authorization") != f"Zoho-oauthtoken {self.valid_token}":
            return FakeResponse(401, {"code": "INVALID_TOKEN"})
        if self.fail_path and path.startswith(self.fail_path):
            return FakeResponse(400, {"code": "INVALID_DATA", "message": "boom"})
        if path == "settings/fields":
            fields = [
                {"api_name": a, "data_type": t, "field_label": label}
                for a, t, label in FIELD_META[params["module"]]
            ]
            return FakeResponse(200, {"fields": fields})
        if path.endswith("/deleted"):
            items = self.deleted.get(path.split("/")[0], [])
            return (
                FakeResponse(204)
                if not items
                else FakeResponse(200, {"data": items, "info": {"more_records": False}})
            )
        if "/Attachments/" in path:
            att = next(a for a in self.attachments if path.endswith(a["id"]))
            return FakeResponse(200, content=att["_bytes"])
        if "fields" not in params:
            return FakeResponse(400, {"code": "REQUIRED_PARAM_MISSING"})
        if len(params["fields"].split(",")) > 50:
            return FakeResponse(400, {"code": "LIMIT_EXCEEDED"})
        items = {"Notes": self.notes, "Attachments": self.attachments}.get(path)
        if items is None:
            items = self.records[path]
        since = headers.get("If-Modified-Since")
        if since:  # inclusive, like Zoho
            items = [i for i in items if i.get("Modified_Time", "") >= since]
            if not items:
                return FakeResponse(304)
        if not items:
            return FakeResponse(204)
        start = int(params.get("page_token") or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        wanted = set(params["fields"].split(","))
        data = [{k: v for k, v in i.items() if k in wanted or k == "id"} for i in chunk]
        info = {
            "more_records": more,
            "next_page_token": str(start + self.page_size) if more else None,
        }
        return FakeResponse(200, {"data": data, "info": info})

    def paths(self, prefix=""):
        return [p for p, _, _ in self.calls if p.startswith(prefix)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(zc.time, "sleep", lambda _s: None)


def _source(fake, **kw):
    kw.setdefault("client_id", "cid")
    kw.setdefault("client_secret", "sec")
    kw.setdefault("refresh_token", "ref")
    kw.setdefault("region", "eu")
    kw.setdefault("modules", ["Leads", "Deals"])
    return zoho_crm_source(client=fake, **kw)


# ---------------------------------------------------------------------------
# DB-free
# ---------------------------------------------------------------------------


def _api(fake):
    token = zc._Token(fake, "https://accounts.zoho.eu", "c", "s", "r")
    return zc._Api(fake, token)


def test_fields_drop_volatile_images_and_contact_details_and_put_id_first():
    names = [f.api_name for f in _module_fields(_api(FakeZoho()), "Leads", redact=True)]
    assert names[0] == "id"
    assert {"Company", "Full_Name", "Lead_Status", "Owner", "Description"} <= set(names)
    for dropped in ("Email", "Phone", "Modified_Time", "Last_Activity_Time", "Record_Image"):
        assert dropped not in names


def test_contact_details_kept_when_redaction_off():
    names = {f.api_name for f in _module_fields(_api(FakeZoho()), "Leads", redact=False)}
    assert {"Email", "Phone"} <= names


def test_fields_are_capped_at_fifty(monkeypatch):
    many = [(f"F{i}", "text", f"F{i}") for i in range(70)] + [("id", "bigint", "ID")]
    monkeypatch.setitem(FIELD_META, "Leads", many)
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    assert len(fields) == 50
    assert fields[0].api_name == "id"


def test_record_row_is_readable_and_stable():
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    row = _record_row("Leads", _lead("1"), fields)
    assert row["id"] == "Leads:1"
    assert row["title"] == "Ann Lee"
    for expected in (
        "Module: Leads",
        "Lead Owner: Sam Rep",
        "Company: Acme",
        "Lead Status: Contacted",
    ):
        assert expected in row["content"]
    for hidden in ("rep@example.com", "ann@example.com", "555", "2026-01-01"):
        assert hidden not in row["content"]
    later = _record_row("Leads", _lead("1", modified="2027-01-01T00:00:00+00:00"), fields)
    assert later == row  # timestamp-only change -> same content -> no re-cognify


def test_render_value_handles_zoho_shapes():
    assert _render_value({"name": "Acme", "id": "9"}) == "Acme"
    assert _render_value([{"name": "a"}, {"name": "b"}]) == "a, b"
    assert _render_value(True) == "yes"
    assert _render_value(None) == ""


def test_row_becomes_a_cognee_document_tagged_zoho_crm():
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    row = _record_row("Leads", _lead("1"), fields)
    dlt_row = SimpleNamespace(row_data=row, content_hash="h", table_name=RECORDS_TABLE)
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), ZOHO_SOURCE_NAME)
    meta = getattr(item, "system_metadata", None) or item.external_metadata
    assert meta["source"] == ZOHO_SOURCE_NAME
    assert item.data.startswith("# Ann Lee")


def test_wrong_region_gives_a_clear_error():
    fake = FakeZoho({"Leads": [_lead("1")]})
    with pytest.raises(PermissionError, match="region"):
        zc._Token(fake, "https://accounts.zoho.com", "c", "s", "r").refresh()


def test_expired_token_is_refreshed_once():
    fake = FakeZoho({"Leads": [_lead("1")]})
    api = _api(fake)
    api.get("settings/fields", {"module": "Leads"})
    fake.valid_token = "rotated"
    api.get("settings/fields", {"module": "Leads"})
    assert len(fake.token_posts) == 2


def test_rate_limit_is_retried():
    fake = FakeZoho({"Leads": [_lead("1")]})
    real_get, left = fake.get, [1]

    def flaky(url, params=None, headers=None):
        if left[0]:
            left[0] -= 1
            return FakeResponse(429, headers={"Retry-After": "1"})
        return real_get(url, params, headers)

    fake.get = flaky
    assert _api(fake).get("settings/fields", {"module": "Leads"})["fields"]


def test_source_configuration_and_validation(monkeypatch):
    for var in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_REGION"):
        monkeypatch.delenv(var, raising=False)
    source = _source(FakeZoho(), include_attachments=True)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == ZOHO_SOURCE_NAME
    assert set(source.resources) == {RECORDS_TABLE, NOTES_TABLE, ATTACHMENTS_TABLE}
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource._hints["columns"]["_deleted"]["hard_delete"] is True
    assert set(_source(FakeZoho(), include_notes=False).resources) == {RECORDS_TABLE}
    with pytest.raises(ValueError, match="credentials"):
        zoho_crm_source(client=FakeZoho())
    with pytest.raises(ValueError, match="region"):
        _source(FakeZoho(), region="mars")


# ---------------------------------------------------------------------------
# dlt pipeline (temp sqlite)
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="zoho_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'z.db'}"),
        dataset_name="zoho",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, fake, **kw):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(fake, **kw))
    return pipeline


def _rows(pipeline, table=RECORDS_TABLE):
    query = f"SELECT id, content FROM {table} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def _state(pipeline, table):
    return pipeline.state["sources"][ZOHO_SOURCE_NAME]["resources"][table]


def _deals():
    return [
        {
            "id": "d1",
            "Deal_Name": "Big deal",
            "Amount": 5000,
            "Stage": "Qualification",
            "Account_Name": {"name": "Acme", "id": "a1"},
            "Modified_Time": "2026-01-01T00:00:00+00:00",
        },
    ]


def test_first_run_pages_through_every_module(tmp_path):
    fake = FakeZoho({"Leads": [_lead(str(i)) for i in range(5)], "Deals": _deals()})
    rows = _rows(_run(tmp_path, fake))
    assert len(rows) == 6
    assert "Account Name: Acme" in rows["Deals:d1"]
    assert any(p.get("page_token") for path, p, _ in fake.calls if path == "Leads")
    assert not fake.paths("Leads/deleted")  # nothing to forget on a first run


def test_second_run_sends_cursor_and_applies_only_changes(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2", name="Bo Kim")], "Deals": _deals()})
    pipeline = _run(tmp_path, fake)
    cursor = _state(pipeline, RECORDS_TABLE)["cursors"]["Leads"]

    fake.records["Leads"][0] = _lead(
        "1", Lead_Status="Qualified", modified="2999-01-01T00:00:00+00:00"
    )
    fake.calls.clear()
    rows = _rows(_run(tmp_path, fake))

    lead_calls = [h for path, _, h in fake.calls if path == "Leads"]
    assert lead_calls[0]["If-Modified-Since"] == cursor
    assert "Lead Status: Qualified" in rows["Leads:1"]
    assert "Leads:2" in rows and "Deals:d1" in rows  # 304 / unchanged rows stay


def test_deleted_records_are_forgotten(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": _deals()})
    _run(tmp_path, fake)
    fake.records["Leads"] = [_lead("2")]
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    rows = _rows(_run(tmp_path, fake))
    assert "Leads:1" not in rows and "Leads:2" in rows


def test_old_deletions_before_cursor_are_ignored(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1")], "Deals": _deals()})
    _run(tmp_path, fake)
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2000-01-01T00:00:00+00:00"}]
    assert "Leads:1" in _rows(_run(tmp_path, fake))


def test_notes_sync_filter_and_forget_with_parent(tmp_path):
    fake = FakeZoho(
        {"Leads": [_lead("1"), _lead("2")], "Deals": _deals()},
        notes=[_note("n1", "1"), _note("n2", "2"), _note("n3", "c9", module="Contacts")],
    )
    pipeline = _run(tmp_path, fake)
    notes = _rows(pipeline, NOTES_TABLE)
    assert set(notes) == {"Notes:n1", "Notes:n2"}  # Contacts not selected
    assert "Note on Lead: Ann Lee" in notes["Notes:n1"]
    assert "rep@example.com" not in notes["Notes:n1"]

    fake.notes = [_note("n2", "2")]
    fake.deleted["Notes"] = [{"id": "n2", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline = _run(tmp_path, fake)
    assert _rows(pipeline, NOTES_TABLE) == {}  # n2 deleted itself, n1 via its deleted lead
    assert "Leads:1" not in _rows(pipeline)


def test_text_attachments_only_and_size_cap(tmp_path):
    def att(aid, name, data, size=None):
        return {
            "id": aid,
            "File_Name": name,
            "Size": size if size is not None else len(data),
            "Parent_Id": {"module": {"api_name": "Deals"}, "name": "Big deal", "id": "d1"},
            "Modified_Time": "2026-01-01T00:00:00+00:00",
            "_bytes": data,
        }

    fake = FakeZoho(
        {"Leads": [], "Deals": _deals()},
        attachments=[
            att("t1", "terms.txt", b"Net 30 payment terms"),
            att("p1", "logo.png", b"\x89PNG"),
            att("b1", "huge.csv", b"x", size=10_000_000),
        ],
    )
    rows = _rows(_run(tmp_path, fake, include_attachments=True), ATTACHMENTS_TABLE)
    assert set(rows) == {"Attachments:t1"}
    assert "Net 30 payment terms" in rows["Attachments:t1"]
    assert "Attachment on Big deal: terms.txt" in rows["Attachments:t1"]


def test_failed_run_keeps_cursor_and_memory(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1")], "Deals": _deals()})
    pipeline = _run(tmp_path, fake)
    cursors = dict(_state(pipeline, RECORDS_TABLE)["cursors"])
    fake.fail_path = "Deals"
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)
    pipeline = _pipeline(tmp_path)
    assert _state(pipeline, RECORDS_TABLE)["cursors"] == cursors
    assert set(_rows(pipeline)) == {"Leads:1", "Deals:d1"}
