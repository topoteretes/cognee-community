"""Unit tests for the Zoho CRM dlt connector (no credentials or network needed).

The fake follows the v8 docs and what a live account showed: ``fields`` is
mandatory and capped at 50, records page with ``next_page_token``, ``GET /Notes``
pages with ``page`` numbers (no token, as documented), ``If-Modified-Since`` is
inclusive and answers ``304``, the deleted feed takes ``If-Modified-Since``,
attachments are listed per record, a deleted record's ``GET`` answers ``204``,
responses carry a ``Date`` header, and a wrong region answers ``invalid_client``.
"""

from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_zoho_crm import zoho_crm as zc
from cognee_community_connector_zoho_crm.zoho_crm import (
    ZOHO_SOURCE_NAME,
    _iter_pages,
    _module_fields,
    _record_row,
    _render_value,
    _UnstableListingError,
    zoho_crm_source,
)

API = "https://www.zohoapis.eu"
WEB = "https://crm.zoho.eu"
SERVER_NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-01-01T00:00:00+00:00"

FIELD_META = {
    "Leads": [
        ("Owner", "ownerlookup", "Lead Owner"),
        ("Company", "text", "Company"),
        ("Full_Name", "text", "Lead Name"),
        ("Email", "email", "Email"),
        ("Phone", "phone", "Phone"),
        ("Lead_Status", "picklist", "Lead Status"),
        ("Interests", "multiselectpicklist", "Interests"),
        ("Created_Time", "datetime", "Created Time"),
        ("Modified_Time", "datetime", "Modified Time"),
        ("Last_Visited_Time", "datetime", "Last Visited Time"),
        ("Days_Visited", "integer", "Days Visited"),
        ("Visitor_Score", "bigint", "Visitor Score"),
        ("Score_Formula", "formula", "Score"),
        ("Record_Image", "profileimage", "Lead Image"),
        ("Description", "textarea", "Description"),
        ("Converted__s", "boolean", "Converted"),
        ("id", "bigint", "ID"),
    ],
    "Deals": [
        ("Deal_Name", "text", "Deal Name"),
        ("Amount", "currency", "Amount"),
        ("Stage", "picklist", "Stage"),
        ("Closing_Date", "date", "Closing Date"),
        ("Account_Name", "lookup", "Account Name"),
        ("id", "bigint", "ID"),
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"Date": format_datetime(SERVER_NOW, usegmt=True), **(headers or {})}

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _lead(rid, name="Ann Lee", modified=OLD, **kw):
    lead = {
        "id": rid,
        "Owner": {"name": "Sam Rep", "id": "u1", "email": "rep@example.com"},
        "Company": "Acme",
        "Full_Name": name,
        "Email": "ann@example.com",
        "Phone": "+1 555 0100",
        "Lead_Status": "Contacted",
        "Interests": ["Support", "Billing"],
        "Created_Time": OLD,
        "Modified_Time": modified,
        "Last_Visited_Time": "2026-10-19T00:00:00+00:00",
        "Days_Visited": 12,
        "Visitor_Score": 80,
        "Score_Formula": 3,
        "Description": None,
        "Converted__s": False,
    }
    lead.update(kw)
    return lead


def _deal(rid="d1", modified=OLD, **kw):
    deal = {
        "id": rid,
        "Deal_Name": "Big deal",
        "Amount": 5000,
        "Stage": "Qualification",
        "Closing_Date": "2026-12-01",
        "Account_Name": {"name": "Acme", "id": "a1"},
        "Modified_Time": modified,
    }
    deal.update(kw)
    return deal


def _note(nid, parent_id, module="Leads", modified=OLD):
    return {
        "id": nid,
        "Note_Title": "Call",
        "Note_Content": f"Discussed pricing ({nid})",
        "Parent_Id": {"module": {"api_name": module}, "name": "Ann Lee", "id": parent_id},
        "Owner": {"name": "Sam Rep", "email": "rep@example.com"},
        "Created_Time": OLD,
        "Modified_Time": modified,
    }


class FakeZoho:
    """In-memory Zoho CRM v8."""

    def __init__(self, records=None, notes=(), attachments=None, page_size=2):
        self.records = {m: list(v) for m, v in (records or {}).items()}
        self.notes = list(notes)
        self.attachments = dict(attachments or {})  # (module, record_id) -> [att incl _bytes]
        self.deleted = {}  # module -> [{"id", "deleted_time"}]
        self.page_size = page_size
        self.calls = []
        self.token_posts = []
        self.valid_token = None
        self.fail_path = None
        self.region_ok = "https://accounts.zoho.eu"
        self.note_tokens = False  # the docs: GET /Notes pages by number, no token

    def post(self, url, params=None):
        self.token_posts.append(url)
        if not url.startswith(self.region_ok):
            return FakeResponse(200, {"error": "invalid_client"})
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
            return FakeResponse(400, {"code": "INVALID_DATA", "message": "secret body text"})
        if path == "settings/fields":
            meta = FIELD_META[params["module"]]
            fields = [{"api_name": a, "data_type": t, "field_label": lbl} for a, t, lbl in meta]
            return FakeResponse(200, {"fields": fields})
        since = headers.get("If-Modified-Since")
        if path.endswith("/deleted"):
            items = self.deleted.get(path.split("/")[0], [])
            if since:
                items = [i for i in items if i["deleted_time"] >= since]
                if not items:
                    return FakeResponse(304)
            if not items:
                return FakeResponse(204)
            return FakeResponse(200, {"data": items, "info": {"more_records": False}})
        parts = path.split("/")
        if len(parts) == 4 and parts[2] == "Attachments":  # download
            att = next(a for a in self.attachments[(parts[0], parts[1])] if a["id"] == parts[3])
            return FakeResponse(200, content=att["_bytes"])
        if "fields" not in params:
            return FakeResponse(400, {"code": "REQUIRED_PARAM_MISSING"})
        if len(params["fields"].split(",")) > 50:
            return FakeResponse(400, {"code": "LIMIT_EXCEEDED"})
        if len(parts) == 3 and parts[2] == "Attachments":
            items = [
                {k: v for k, v in a.items() if not k.startswith("_")}
                for a in self.attachments.get((parts[0], parts[1]), [])
            ]
            return self._page(items, params, use_token=False)
        if len(parts) == 2:  # single record
            rec = next((r for r in self.records.get(parts[0], []) if r["id"] == parts[1]), None)
            return FakeResponse(204) if rec is None else FakeResponse(200, {"data": [rec]})
        items = self.notes if path == "Notes" else self.records[path]
        if path in self.records and path == "Leads":
            items = [r for r in items if not r.get("Converted__s")]  # converted leave Leads
        if since:  # inclusive, like Zoho
            items = [i for i in items if i.get("Modified_Time", "") >= since]
            if not items:
                return FakeResponse(304)
        wanted = set(params["fields"].split(","))
        items = [{k: v for k, v in i.items() if k in wanted or k == "id"} for i in items]
        return self._page(items, params, use_token=path != "Notes" or self.note_tokens)

    def _page(self, items, params, use_token):
        if not items:
            return FakeResponse(204)
        size = min(int(params.get("per_page", 200)), 200, self.page_size)
        if "page_token" in params:
            start = int(params["page_token"])
        else:
            start = (int(params.get("page", 1)) - 1) * size
        chunk = items[start : start + size]
        more = start + size < len(items)
        info = {"more_records": more, "per_page": size}
        if use_token:
            info["next_page_token"] = str(start + size) if more else None
        return FakeResponse(200, {"data": chunk, "info": info})

    def paths(self, prefix):
        return [(p, q, h) for p, q, h in self.calls if p.startswith(prefix)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(zc.time, "sleep", lambda _s: None)


def _src(fake, **kw):
    kw.setdefault("client_id", "cid")
    kw.setdefault("client_secret", "sec")
    kw.setdefault("refresh_token", "ref")
    kw.setdefault("region", "eu")
    kw.setdefault("modules", ["Leads", "Deals"])
    return zoho_crm_source(service=fake, **kw)


def _api(fake, stats=None, max_wait=120.0):
    token = zc._Token(fake, "https://accounts.zoho.eu", "c", "s", "r")
    return zc._Api(fake, token, stats if stats is not None else {}, None, max_wait)


# ---------------------------------------------------------------------------
# DB-free
# ---------------------------------------------------------------------------


def test_fields_dropped_by_type_and_name_redacted_and_id_first():
    names = [f.api_name for f in _module_fields(_api(FakeZoho()), "Leads", redact=True)]
    assert names[0] == "id"
    assert {"Company", "Full_Name", "Lead_Status", "Owner", "Interests", "Created_Time"} <= set(
        names
    )
    for dropped in (
        "Email",
        "Phone",
        "Modified_Time",
        "Last_Visited_Time",
        "Days_Visited",
        "Visitor_Score",
        "Score_Formula",
        "Record_Image",
    ):
        assert dropped not in names


def test_fields_are_capped_at_fifty(monkeypatch):
    many = [(f"F{i}", "text", f"F{i}") for i in range(70)] + [("id", "bigint", "ID")]
    monkeypatch.setitem(FIELD_META, "Leads", many)
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    assert len(fields) == 50 and fields[0].api_name == "id"


def test_record_row_is_stable_against_visits_and_multiselect_order():
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    row = _record_row("Leads", _lead("1"), fields, WEB)
    assert row["url"] == f"{WEB}/crm/tab/Leads/1"
    assert "Interests: Billing, Support" in row["content"]
    for hidden in ("rep@example.com", "ann@example.com", "555", "Visitor", "Days Visited"):
        assert hidden not in row["content"]
    visited = _lead(
        "1",
        Days_Visited=13,
        Visitor_Score=99,
        Interests=["Billing", "Support"],
        Last_Visited_Time="2026-10-20T00:00:00+00:00",
        modified="2027-01-01",
    )
    assert _record_row("Leads", visited, fields, WEB) == row


def test_render_value_handles_zoho_shapes():
    assert _render_value({"name": "Acme", "id": "9"}) == "Acme"
    assert _render_value([{"name": "b"}, {"name": "a"}]) == "a, b"
    assert _render_value(True) == "yes"
    assert _render_value(None) == ""


def test_document_is_tagged_zoho_crm():
    fields = _module_fields(_api(FakeZoho()), "Leads", redact=True)
    row = _record_row("Leads", _lead("1"), fields, WEB)
    dlt_row = SimpleNamespace(row_data=row, content_hash="h", table_name="zoho_crm_records")
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), ZOHO_SOURCE_NAME)
    assert item.system_metadata["source"] == ZOHO_SOURCE_NAME
    assert item.system_metadata["url"] == f"{WEB}/crm/tab/Leads/1"


def test_wrong_region_gives_a_clear_error():
    fake = FakeZoho()
    with pytest.raises(PermissionError, match="region"):
        zc._Token(fake, "https://accounts.zoho.com", "c", "s", "r").refresh()


def test_expired_token_is_refreshed_once():
    fake = FakeZoho({"Leads": [_lead("1")]})
    api = _api(fake)
    api.get("settings/fields", {"module": "Leads"})
    fake.valid_token = "rotated"
    api.get("settings/fields", {"module": "Leads"})
    assert len(fake.token_posts) == 2


def test_errors_carry_status_and_code_but_not_the_body():
    fake = FakeZoho({"Leads": [_lead("1")]})
    fake.fail_path = "Leads"
    with pytest.raises(RuntimeError) as info:
        _api(fake).get("Leads", {"fields": "id"})
    assert "400" in str(info.value) and "INVALID_DATA" in str(info.value)
    assert "secret body text" not in str(info.value)


def test_long_retry_after_stops_without_sleeping(monkeypatch):
    slept, calls = [], []
    monkeypatch.setattr(zc.time, "sleep", slept.append)
    fake = FakeZoho()
    fake.valid_token = None
    real_get = fake.get

    def limited(url, params=None, headers=None):
        if "settings" in url:
            calls.append(1)
            return FakeResponse(429, headers={"Retry-After": "900"})
        return real_get(url, params, headers)

    fake.get = limited
    with pytest.raises(zc.RateLimitedError):
        _api(fake).get("settings/fields", {"module": "Leads"})
    assert slept == [] and len(calls) == 1


def test_short_retry_after_is_retried():
    fake = FakeZoho()
    real_get, left = fake.get, [1]

    def flaky(url, params=None, headers=None):
        if left[0]:
            left[0] -= 1
            return FakeResponse(429, headers={"Retry-After": "1"})
        return real_get(url, params, headers)

    fake.get = flaky
    assert _api(fake).get("settings/fields", {"module": "Leads"})["fields"]


def test_notes_paged_by_page_number_as_documented():
    fake = FakeZoho(notes=[_note(f"n{i}", "1") for i in range(450)], page_size=200)
    rows = list(_iter_pages(_api(fake), "Notes", {"fields": "id"}))
    assert len(rows) == 450
    assert [q.get("page") for _, q, _ in fake.paths("Notes")] == [None, 2, 3]


def test_repeated_page_token_raises():
    class Api:
        def get(self, path, params=None, headers=None):
            n = len(params.get("page_token", ""))
            return {"data": [{"id": n}], "info": {"more_records": True, "next_page_token": "same"}}

    with pytest.raises(_UnstableListingError):
        list(_iter_pages(Api(), "Leads", {"fields": "id"}))


def test_source_configuration_and_validation():
    source = _src(FakeZoho(), resource_name="zoho_acme", include_attachments=True)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == ZOHO_SOURCE_NAME
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "zoho_acme"
    assert set(source.resources) == {
        "zoho_acme_records",
        "zoho_acme_notes",
        "zoho_acme_attachments",
    }
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource._hints["columns"]["_deleted"]["hard_delete"] is True
    assert isinstance(source.cognee_sync_stats, dict)
    fake = FakeZoho()
    assert zoho_crm_source(client=fake, client_id="c", client_secret="s", refresh_token="r")
    with pytest.raises(TypeError):
        zoho_crm_source("c", "s", "r")  # keyword-only
    with pytest.raises(TypeError):
        zoho_crm_source(service=fake)  # credentials are required arguments, no env fallback
    with pytest.raises(ValueError, match="region"):
        _src(fake, region="mars")
    with pytest.raises(ValueError, match="resource_name"):
        _src(fake, resource_name="Bad Name")


def test_old_cognee_core_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"1\.6\.1"):
        _src(FakeZoho())


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
    source = _src(fake, **kw)
    pipeline.run(source)
    return pipeline, source


def _rows(pipeline, table="zoho_crm_records"):
    query = f"SELECT id, content FROM {table} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def _state(pipeline, table="zoho_crm_records", name="zoho_crm"):
    return pipeline.state["sources"][name]["resources"][table]


def test_first_run_pages_every_module_and_uses_server_clock(tmp_path):
    fake = FakeZoho({"Leads": [_lead(str(i)) for i in range(5)], "Deals": [_deal()]})
    pipeline, source = _run(tmp_path, fake)
    rows = _rows(pipeline)
    assert len(rows) == 6 and "Account Name: Acme" in rows["Deals:d1"]
    assert any(q.get("page_token") for p, q, _ in fake.calls if p == "Leads")
    assert _state(pipeline)["cursors"] == {
        "Leads": "2026-10-20T11:55:00+00:00",
        "Deals": "2026-10-20T11:55:00+00:00",
    }
    assert source.cognee_sync_stats["records_synced"] == 6


def test_second_run_sends_cursor_everywhere_and_applies_only_changes(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2", name="Bo Kim")], "Deals": [_deal()]})
    _run(tmp_path, fake)
    fake.records["Leads"][0] = _lead("1", Lead_Status="Qualified", modified="2999-01-01")
    fake.calls.clear()
    pipeline, _ = _run(tmp_path, fake)
    rows = _rows(pipeline)
    cursor = "2026-10-20T11:55:00+00:00"
    assert next(h.get("If-Modified-Since") for p, _, h in fake.calls if p == "Leads") == cursor
    assert all(h.get("If-Modified-Since") == cursor for p, _, h in fake.paths("Leads/deleted"))
    assert "Lead Status: Qualified" in rows["Leads:1"]
    assert "Leads:2" in rows and "Deals:d1" in rows


def test_no_cursor_moves_if_a_later_module_fails(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1")], "Deals": [_deal()]})
    pipeline, _ = _run(tmp_path, fake)
    cursors = dict(_state(pipeline)["cursors"])
    fake.fail_path = "Deals"  # Leads finishes, then Deals fails
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)
    pipeline = _pipeline(tmp_path)
    assert _state(pipeline)["cursors"] == cursors
    assert set(_rows(pipeline)) == {"Leads:1", "Deals:d1"}


def test_deleted_records_are_forgotten(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": [_deal()]})
    _run(tmp_path, fake)
    fake.records["Leads"] = [_lead("2")]
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline, source = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"Leads:2", "Deals:d1"}
    assert source.cognee_sync_stats["records_forgotten"] == 1


def test_converted_lead_is_forgotten_by_reconcile(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": [_deal()]})
    _run(tmp_path, fake)
    fake.records["Leads"][0]["Converted__s"] = True  # leaves Leads, no delete event
    pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"Leads:2", "Deals:d1"}


def test_live_record_missing_from_listing_is_kept_after_recheck(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": [_deal()]})
    _run(tmp_path, fake)
    real_page = fake._page

    def hide_two(items, params, use_token):
        if params.get("sort_by") == "id":
            items = [i for i in items if i["id"] != "2"]
        return real_page(items, params, use_token)

    fake._page = hide_two
    pipeline, _ = _run(tmp_path, fake)
    assert "Leads:2" in _rows(pipeline)  # single GET says it is alive


def test_reconcile_every_n_runs(tmp_path):
    fake = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": [_deal()]})
    _run(tmp_path, fake, reconcile_every=2)
    fake.records["Leads"][0]["Converted__s"] = True
    pipeline, _ = _run(tmp_path, fake, reconcile_every=2)
    assert "Leads:1" in _rows(pipeline)  # run 1 of 2: no id listing
    pipeline, _ = _run(tmp_path, fake, reconcile_every=2)
    assert "Leads:1" not in _rows(pipeline)


def test_module_removed_is_forgotten_and_module_added_backfills_notes(tmp_path):
    fake = FakeZoho(
        {"Leads": [_lead("1")], "Deals": [_deal()]},
        notes=[_note("n1", "1"), _note("n2", "d1", module="Deals")],
    )
    pipeline, _ = _run(tmp_path, fake, modules=["Leads"])
    assert set(_rows(pipeline)) == {"Leads:1"}
    assert set(_rows(pipeline, "zoho_crm_notes")) == {"Notes:n1"}
    pipeline, _ = _run(tmp_path, fake, modules=["Leads", "Deals"])  # add Deals later
    assert set(_rows(pipeline)) == {"Leads:1", "Deals:d1"}
    assert set(_rows(pipeline, "zoho_crm_notes")) == {"Notes:n1", "Notes:n2"}  # old note too
    pipeline, _ = _run(tmp_path, fake, modules=["Deals"])  # drop Leads
    assert set(_rows(pipeline)) == {"Deals:d1"}
    assert set(_rows(pipeline, "zoho_crm_notes")) == {"Notes:n2"}


def test_notes_sync_filter_and_forget_with_parent(tmp_path):
    fake = FakeZoho(
        {"Leads": [_lead("1"), _lead("2")], "Deals": [_deal()]},
        notes=[_note("n1", "1"), _note("n2", "2"), _note("n3", "c9", module="Contacts")],
    )
    pipeline, _ = _run(tmp_path, fake)
    notes = _rows(pipeline, "zoho_crm_notes")
    assert set(notes) == {"Notes:n1", "Notes:n2"}
    assert (
        "Note on Lead: Ann Lee" in notes["Notes:n1"] and "rep@example.com" not in notes["Notes:n1"]
    )
    fake.notes = [_note("n2", "2")]
    fake.deleted["Notes"] = [{"id": "n2", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    fake.records["Leads"] = [_lead("2")]
    pipeline, _ = _run(tmp_path, fake)
    assert _rows(pipeline, "zoho_crm_notes") == {}


def _att(aid, name, data, size=None):
    return {
        "id": aid,
        "File_Name": name,
        "Size": size if size is not None else len(data),
        "Modified_Time": OLD,
        "_bytes": data,
    }


def test_attachments_listed_per_record_text_only_with_size_cap(tmp_path):
    fake = FakeZoho(
        {"Leads": [], "Deals": [_deal()]},
        attachments={
            ("Deals", "d1"): [
                _att("t1", "terms.txt", b"Net 30 payment terms"),
                _att("p1", "logo.png", b"\x89PNG"),
                _att("b1", "huge.csv", b"x", size=10_000_000),
            ]
        },
    )
    pipeline, source = _run(tmp_path, fake, include_attachments=True)
    rows = _rows(pipeline, "zoho_crm_attachments")
    assert set(rows) == {"Attachments:t1"} and "Net 30 payment terms" in rows["Attachments:t1"]
    assert not fake.paths("Attachments")  # no global /Attachments module
    assert fake.paths("Deals/d1/Attachments")
    assert source.cognee_sync_stats["attachments_skipped"] == 2


def test_attachment_removed_or_parent_deleted_is_forgotten(tmp_path):
    fake = FakeZoho(
        {"Leads": [], "Deals": [_deal("d1"), _deal("d2")]},
        attachments={
            ("Deals", "d1"): [_att("t1", "a.txt", b"one"), _att("t2", "b.txt", b"two")],
            ("Deals", "d2"): [_att("t3", "c.txt", b"three")],
        },
    )
    _run(tmp_path, fake, include_attachments=True)
    fake.attachments[("Deals", "d1")] = [_att("t1", "a.txt", b"one")]  # t2 removed
    fake.records["Deals"][0]["Modified_Time"] = "2999-01-01"  # d1 re-listed
    fake.records["Deals"] = [r for r in fake.records["Deals"] if r["id"] != "d2"]
    fake.deleted["Deals"] = [{"id": "d2", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline, _ = _run(tmp_path, fake, include_attachments=True)
    assert set(_rows(pipeline, "zoho_crm_attachments")) == {"Attachments:t1"}


def test_long_rate_limit_stops_cleanly_and_moves_no_cursor(tmp_path, monkeypatch):
    """A clean stop keeps dlt state, so no cursor may move before every module is done."""
    fake = FakeZoho({"Leads": [_lead("1")], "Deals": [_deal()]})
    pipeline, _ = _run(tmp_path, fake)
    cursors = dict(_state(pipeline)["cursors"])
    notes_cursor = _state(pipeline, "zoho_crm_notes")["cursor"]
    later = datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setitem(globals(), "SERVER_NOW", later)  # Zoho's clock moved on
    real_get = fake.get

    def limited(url, params=None, headers=None):
        if "/Deals" in url and "settings" not in url:  # Leads finishes, Deals is limited
            return FakeResponse(429, headers={"Retry-After": "900"})
        return real_get(url, params, headers)

    fake.get = limited
    pipeline, source = _run(tmp_path, fake)  # no exception
    assert source.cognee_sync_stats["failed_rate_limit"] == 1  # notes step skipped
    assert _state(pipeline)["cursors"] == cursors  # not even the finished Leads cursor
    assert _state(pipeline, "zoho_crm_notes")["cursor"] == notes_cursor


def test_two_sources_in_one_dataset_keep_separate_state(tmp_path):
    acme = FakeZoho({"Leads": [_lead("1"), _lead("2")], "Deals": []})
    globex = FakeZoho({"Leads": [_lead("7")], "Deals": []})
    pipeline = _pipeline(tmp_path)
    pipeline.run(_src(acme, resource_name="zoho_acme"))
    pipeline.run(_src(globex, resource_name="zoho_globex"))
    acme.records["Leads"] = [acme.records["Leads"][0]]
    acme.deleted["Leads"] = [{"id": "2", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline.run(_src(acme, resource_name="zoho_acme"))
    pipeline.run(_src(globex, resource_name="zoho_globex"))
    assert set(_rows(pipeline, "zoho_acme_records")) == {"Leads:1"}
    assert set(_rows(pipeline, "zoho_globex_records")) == {"Leads:7"}
