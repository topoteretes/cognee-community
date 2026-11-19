"""Unit tests for the Zoho CRM dlt connector (no credentials or network needed).

The fake follows the v8 docs and what a live account showed: ``fields`` is
mandatory and capped at 50, records page with ``next_page_token``, ``GET /Notes``
pages with ``page`` numbers (no token, as documented), ``If-Modified-Since`` is
inclusive and answers ``304``, the deleted feed takes ``If-Modified-Since``,
attachments are listed per record, a deleted record's ``GET`` answers ``204``,
responses carry a ``Date`` header, and a wrong region answers ``invalid_client``.
"""

import time
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


def test_repeated_page_token_raises_before_any_duplicate_id():
    class Api:
        calls = 0

        def get(self, path, params=None, headers=None):
            self.calls += 1
            return {
                "data": [{"id": self.calls}],  # unique: only the repeated token can fail
                "info": {"more_records": True, "next_page_token": "same"},
            }

    with pytest.raises(_UnstableListingError, match="repeated a page token"):
        list(_iter_pages(Api(), "Leads", {"fields": "id"}))


def test_check_active_runs_before_each_request_and_can_abort():
    fake = FakeZoho()
    checks = []
    token = zc._Token(fake, "https://accounts.zoho.eu", "c", "s", "r")
    api = zc._Api(fake, token, {}, lambda: checks.append("checked"), 120)
    api.get("settings/fields", {"module": "Leads"})
    api.get("settings/fields", {"module": "Deals"})
    assert len(checks) == 2

    def inactive():
        raise PermissionError("inactive")

    stopped = zc._Api(fake, token, {}, inactive, 120)
    before = len(fake.calls)
    with pytest.raises(PermissionError, match="inactive"):
        stopped.get("settings/fields", {"module": "Leads"})
    assert len(fake.calls) == before  # checkpoint is before the HTTP request


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
    with pytest.raises(TypeError, match="not a string"):
        _src(fake, modules="Leads")


def test_old_cognee_core_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"1\.6\.1"):
        _src(FakeZoho())


def test_core_without_pipeline_scope_attr_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    monkeypatch.delattr(dlt_utils, "PIPELINE_SCOPE_ATTR")
    with pytest.raises(RuntimeError, match="PIPELINE_SCOPE_ATTR"):
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


def test_more_than_max_verify_converted_leads_make_progress(tmp_path):
    leads = [_lead(str(i)) for i in range(251)]
    fake = FakeZoho({"Leads": leads, "Deals": []}, page_size=200)
    _run(tmp_path, fake)
    for lead in fake.records["Leads"][:250]:
        lead["Converted__s"] = True

    pipeline, source = _run(tmp_path, fake)
    assert len(_rows(pipeline)) == 51  # 200 confirmed and forgotten this run
    assert source.cognee_sync_stats.get("reconcile_skipped", 0) == 0
    pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"Leads:250"}


def test_reconcile_rotates_past_live_ids_to_reach_converted_backlog(tmp_path):
    leads = [_lead(f"{i:03d}") for i in range(260)]
    fake = FakeZoho({"Leads": leads, "Deals": []}, page_size=200)
    _run(tmp_path, fake)
    for lead in fake.records["Leads"][200:]:
        lead["Converted__s"] = True

    real_page = fake._page

    def hide_first_two_hundred(items, params, use_token):
        if params.get("sort_by") == "id":
            items = [item for item in items if int(item["id"]) >= 200]
        return real_page(items, params, use_token)

    fake._page = hide_first_two_hundred
    pipeline, _ = _run(tmp_path, fake)
    assert len(_rows(pipeline)) == 260  # first rotating batch was alive
    assert len(_state(pipeline)["reconcile_live"]["Leads"]) == 200  # remembered alive
    assert "reconcile_positions" not in _state(pipeline)

    pipeline, _ = _run(tmp_path, fake)
    rows = _rows(pipeline)
    assert len(rows) == 200
    assert all(f"Leads:{i:03d}" in rows for i in range(200))


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
    # Change modules while a note deletion is pending. Listing must backfill the
    # added module, while the deleted feed must still use the old cursor.
    fake.notes = [_note("n2", "d1", module="Deals")]
    fake.deleted["Notes"] = [{"id": "n1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline, _ = _run(tmp_path, fake, modules=["Leads", "Deals"])
    assert set(_rows(pipeline)) == {"Leads:1", "Deals:d1"}
    assert set(_rows(pipeline, "zoho_crm_notes")) == {"Notes:n2"}
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


def test_long_rate_limit_stops_interleaved_resources_per_request(tmp_path, monkeypatch):
    """A page-3 stop halts Notes before its next request; neither cursor moves."""
    fake = FakeZoho(
        {"Leads": [_lead(str(i)) for i in range(4)], "Deals": []},
        notes=[_note(f"n{i}", str(i)) for i in range(4)],
        page_size=1,
    )
    pipeline, _ = _run(tmp_path, fake)
    cursors = dict(_state(pipeline)["cursors"])
    notes_cursor = _state(pipeline, "zoho_crm_notes")["cursor"]
    later = datetime(2026, 10, 21, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setitem(globals(), "SERVER_NOW", later)
    for lead in fake.records["Leads"]:
        lead["Modified_Time"] = "2999-01-01T00:00:00+00:00"
    for note in fake.notes:
        note["Modified_Time"] = "2999-01-01T00:00:00+00:00"
    real_get = fake.get
    stopped_at = []

    def limited(url, params=None, headers=None):
        path = urlsplit(url).path
        if path.endswith("/Leads") and (params or {}).get("page_token") == "2":
            stopped_at.append(len(fake.calls))
            return FakeResponse(429, headers={"Retry-After": "900"})
        return real_get(url, params, headers)

    fake.get = limited
    pipeline, source = _run(tmp_path, fake)
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert _state(pipeline)["cursors"] == cursors
    assert _state(pipeline, "zoho_crm_notes")["cursor"] == notes_cursor
    assert stopped_at
    # _Api checks the shared flag before calling the fake again.
    assert not any(path == "Notes" for path, _, _ in fake.calls[stopped_at[0] :])


def test_attachment_fanout_resumes_completed_parents(tmp_path):
    deals = [_deal(str(i)) for i in range(10)]
    fake = FakeZoho({"Leads": [], "Deals": deals}, page_size=10)
    real_get = fake.get
    attachment_calls = []

    def limited(url, params=None, headers=None):
        path = urlsplit(url).path.split(f"/crm/{zc.API_VERSION}/", 1)[1]
        if path.endswith("/Attachments"):
            attachment_calls.append(path)
            if len(attachment_calls) == 4:
                return FakeResponse(429, headers={"Retry-After": "900"})
        return real_get(url, params, headers)

    fake.get = limited
    pipeline, source = _run(tmp_path, fake, include_attachments=True)
    state = _state(pipeline, "zoho_crm_attachments")
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert "cursor" not in state
    assert len(state["progress"]["processed"]["Deals"]) == 3

    # A completed parent changes while the run is waiting to resume. Its newer
    # Modified_Time must invalidate only that parent's checkpoint.
    fake.attachments[("Deals", "0")] = [_att("t9", "new.txt", b"new after stop")]
    fake.records["Deals"][0]["Modified_Time"] = "2999-01-01T00:00:00+00:00"
    fake.get = real_get
    fake.calls.clear()
    pipeline, _ = _run(tmp_path, fake, include_attachments=True)
    calls = [p for p, _, _ in fake.calls if p.endswith("/Attachments")]
    assert len(calls) == 8  # 7 unfinished parents + changed parent 0
    assert set(_rows(pipeline, "zoho_crm_attachments")) == {"Attachments:t9"}
    assert "progress" not in _state(pipeline, "zoho_crm_attachments")


def test_attachment_removed_without_parent_change_is_documented_gap(tmp_path):
    fake = FakeZoho(
        {"Leads": [], "Deals": [_deal()]},
        attachments={("Deals", "d1"): [_att("t1", "a.txt", b"one")]},
    )
    _run(tmp_path, fake, include_attachments=True)
    fake.attachments[("Deals", "d1")] = []  # parent Modified_Time is unchanged
    pipeline, _ = _run(tmp_path, fake, include_attachments=True)
    assert set(_rows(pipeline, "zoho_crm_attachments")) == {"Attachments:t1"}


def test_attachment_dead_parent_checked_when_modules_change(tmp_path):
    fake = FakeZoho(
        {"Leads": [_lead("1")], "Deals": [_deal()]},
        attachments={("Leads", "1"): [_att("t1", "a.txt", b"one")]},
    )
    _run(tmp_path, fake, modules=["Leads"], include_attachments=True)
    fake.records["Leads"] = []
    fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
    pipeline, _ = _run(tmp_path, fake, modules=["Leads", "Deals"], include_attachments=True)
    assert _rows(pipeline, "zoho_crm_attachments") == {}


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


# ---------------------------------------------------------------------------
# Reconcile backlog, checkpoints and state consistency
# ---------------------------------------------------------------------------


def _hide_from_id_listing(fake, hidden):
    """The ``sort_by=id`` listing omits ``hidden`` ids that are still alive."""
    real_page = fake._page

    def page(items, params, use_token):
        if params.get("sort_by") == "id":
            items = [i for i in items if i["id"] not in hidden]
        return real_page(items, params, use_token)

    fake._page = page


def _id_listings(fake):
    return [c for c in fake.calls if c[0] == "Leads" and c[1].get("sort_by") == "id"]


def _single_gets(fake, module="Leads"):
    return [
        p
        for p, _, _ in fake.calls
        if p.startswith(f"{module}/") and p.count("/") == 1 and not p.endswith("/deleted")
    ]


def test_rate_stop_during_reconcile_keeps_verified_ids_and_hides_private_keys(tmp_path):
    fake = FakeZoho({"Leads": [_lead(f"{i:03d}") for i in range(251)], "Deals": []}, page_size=200)
    _run(tmp_path, fake)
    for lead in fake.records["Leads"][:250]:
        lead["Converted__s"] = True
    cursors = dict(_state(_pipeline(tmp_path))["cursors"])
    real_get = fake.get
    singles = []

    def limited(url, params=None, headers=None):
        path = urlsplit(url).path.split(f"/crm/{zc.API_VERSION}/", 1)[1]
        if path.startswith("Leads/") and path.count("/") == 1 and path != "Leads/deleted":
            singles.append(path)
            if len(singles) == 50:
                return FakeResponse(429, headers={"Retry-After": "900"})
        return real_get(url, params, headers)

    fake.get = limited
    pipeline, source = _run(tmp_path, fake)
    stats = source.cognee_sync_stats
    assert stats["failed_rate_limit"] == 1 and stats["records_forgotten"] == 49
    assert not [k for k in stats if k.startswith("_")]
    state = _state(pipeline)
    assert state["cursors"] == cursors  # nothing else moves
    assert len(state["known"]["Leads"]) == 251 - 49
    assert len(_rows(pipeline)) == 251 - 49

    fake.get = real_get
    fake.calls.clear()
    pipeline, source = _run(tmp_path, fake)
    done = {f"Leads/{i:03d}" for i in range(49)}
    assert not done & set(_single_gets(fake))  # not verified twice
    assert source.cognee_sync_stats["records_forgotten"] == 200
    pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"Leads:250"}


def test_live_ids_missing_from_listing_end_the_forced_backlog_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(zc, "MAX_VERIFY", 3)
    fake = FakeZoho({"Leads": [_lead(f"{i:02d}") for i in range(12)], "Deals": []}, page_size=50)
    _run(tmp_path, fake, reconcile_every=3)
    for lead in fake.records["Leads"][8:10]:
        lead["Converted__s"] = True
    _hide_from_id_listing(fake, {f"{i:02d}" for i in range(8)})  # alive, never listed
    _run(tmp_path, fake, reconcile_every=3)  # run 2 of 3: no id listing
    pipeline, _ = _run(tmp_path, fake, reconcile_every=3)
    assert not _id_listings(fake)

    gets_per_run = []
    for _ in range(4):  # one cycle: 10 missing ids, 3 per run
        fake.calls.clear()
        pipeline, source = _run(tmp_path, fake, reconcile_every=3)
        assert len(_id_listings(fake)) == 1  # forced even though reconcile_every=3
        assert not [k for k in source.cognee_sync_stats if k.startswith("_")]
        gets_per_run.append(_single_gets(fake))
        if len(gets_per_run) == 1:
            assert sorted(_state(pipeline)["reconcile_live"]["Leads"]) == ["00", "01", "02"]
    assert gets_per_run == [
        ["Leads/00", "Leads/01", "Leads/02"],
        ["Leads/03", "Leads/04", "Leads/05"],
        ["Leads/06", "Leads/07", "Leads/08"],
        ["Leads/09"],
    ]
    assert set(_rows(pipeline)) == {f"Leads:{i:02d}" for i in range(8)} | {"Leads:10", "Leads:11"}
    assert "reconcile_live" not in _state(pipeline) or not _state(pipeline)["reconcile_live"]

    fake.calls.clear()  # cycle done: back to the configured cadence
    _run(tmp_path, fake, reconcile_every=3)
    _run(tmp_path, fake, reconcile_every=3)
    assert not _id_listings(fake)
    _run(tmp_path, fake, reconcile_every=3)
    assert len(_id_listings(fake)) == 1  # regular reconcile checks the live ids again
    assert _single_gets(fake) == ["Leads/00", "Leads/01", "Leads/02"]  # new cycle


def test_unstable_listing_keeps_the_backlog_cycle_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(zc, "MAX_VERIFY", 2)
    fake = FakeZoho({"Leads": [_lead(f"{i:02d}") for i in range(6)], "Deals": []}, page_size=50)
    _run(tmp_path, fake)
    base_page = fake._page
    _hide_from_id_listing(fake, {f"{i:02d}" for i in range(6)})
    pipeline, _ = _run(tmp_path, fake)
    live = list(_state(pipeline)["reconcile_live"]["Leads"])
    assert live == ["00", "01"]

    def repeated(items, params, use_token):
        if params.get("sort_by") == "id":
            items = [_lead("02"), _lead("02")]
        return base_page(items, params, use_token)

    fake._page = repeated
    fake.calls.clear()
    pipeline, source = _run(tmp_path, fake)
    assert source.cognee_sync_stats["reconcile_skipped"] == 1
    assert _single_gets(fake) == []
    assert _state(pipeline)["reconcile_live"]["Leads"] == live


def test_removed_module_drops_its_reconcile_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(zc, "MAX_VERIFY", 2)
    deals = [_deal(f"d{i}") for i in range(5)]
    fake = FakeZoho({"Leads": [_lead("1")], "Deals": deals}, page_size=50)
    _run(tmp_path, fake)
    _hide_from_id_listing(fake, {f"d{i}" for i in range(5)})
    pipeline, _ = _run(tmp_path, fake)
    assert set(_state(pipeline)["reconcile_live"]) == {"Deals"}
    pipeline, _ = _run(tmp_path, fake, modules=["Leads"])
    assert "Deals" not in _state(pipeline).get("reconcile_live", {})
    assert set(_rows(pipeline)) == {"Leads:1"}


def _attachment_options():
    return zc._Options(modules=("Deals",))


def test_legacy_attachment_checkpoint_is_discarded_not_crashed_on(tmp_path):
    fake = FakeZoho({"Leads": [], "Deals": [_deal("0"), _deal("1")]}, page_size=10)
    state = {"progress": {"modules": ["Deals"], "since": None, "processed": {"Deals": ["0", "1"]}}}
    list(zc._sync_attachments(_api(fake), state, _attachment_options(), {}))
    assert len(fake.paths("Deals/0/Attachments")) == 1  # old list format: nothing skipped
    assert len(fake.paths("Deals/1/Attachments")) == 1
    assert "progress" not in state


def test_attachment_checkpoint_is_updated_in_place_and_scales(monkeypatch):
    parents = [{"id": str(i), "Modified_Time": OLD} for i in range(20_000)]

    def pages(api, path, params, headers=None):
        if path.endswith("/Attachments"):
            return iter(())
        return iter(parents)

    monkeypatch.setattr(zc, "_iter_pages", pages)
    state: dict = {}
    api = SimpleNamespace(server_time=None)
    monkeypatch.setattr(zc, "_new_cursor", lambda _api: "cursor")
    started = time.monotonic()
    list(zc._sync_attachments(api, state, _attachment_options(), {}))  # no stop: all done
    assert time.monotonic() - started < 5  # per-parent sorting took ~18 s here
    assert "progress" not in state and state["cursor"] == "cursor"

    # A stop part-way leaves one shared checkpoint whose entries are complete.
    def stopping(api, path, params, headers=None):
        if path.endswith("/Attachments"):
            if path == "Deals/3/Attachments":
                raise zc.RateLimitedError("stop")
            return iter(())
        return iter(parents[:10])

    monkeypatch.setattr(zc, "_iter_pages", stopping)
    state = {}
    with pytest.raises(zc.RateLimitedError):
        list(zc._sync_attachments(api, state, _attachment_options(), {}))
    assert state["progress"]["processed"]["Deals"] == {"0": OLD, "1": OLD, "2": OLD}


def test_attachment_removal_does_not_scan_every_owner(tmp_path):
    fake = FakeZoho(
        {"Leads": [], "Deals": [_deal("d1"), _deal("d2")]},
        attachments={
            ("Deals", "d1"): [_att("t1", "a.txt", b"one"), _att("t2", "b.txt", b"two")],
            ("Deals", "d2"): [_att("t3", "c.txt", b"three")],
        },
    )
    _run(tmp_path, fake, include_attachments=True)
    fake.attachments[("Deals", "d1")] = [_att("t1", "a.txt", b"one")]
    fake.records["Deals"][0]["Modified_Time"] = "2999-01-01"
    pipeline, _ = _run(tmp_path, fake, include_attachments=True)
    assert set(_rows(pipeline, "zoho_crm_attachments")) == {"Attachments:t1", "Attachments:t3"}
    assert _state(pipeline, "zoho_crm_attachments")["owners"] == {
        "t1": ["Deals", "d1"],
        "t3": ["Deals", "d2"],
    }
