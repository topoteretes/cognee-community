"""Unit tests for the Freshsales dlt connector (no credentials or network needed).

The fake API mirrors behaviour observed on a live Freshsales account:
``filtered_search`` returns *partial* records, ``updated_at is_after`` is strict
with second precision, an unknown operator is silently treated as "equals",
detail calls embed notes/owner/stage via ``?include=``, deleted records sit in a
"Recycle Bin" view, and a deal record carries chat-widget JWT tokens.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_freshsales import freshsales as fs
from cognee_community_connector_freshsales.freshsales import (
    FRESHSALES_SOURCE_NAME,
    RECORDS_TABLE,
    _html_to_text,
    _since,
    base_url,
    freshsales_source,
)

USER = {"id": 7, "display_name": "Sam Rep", "email": "rep@example.com"}
STAGES = [{"id": 1, "name": "New"}, {"id": 2, "name": "Negotiation"}]


def _contact(cid, updated="2026-01-01T00:00:00Z", **kw):
    c = {
        "id": cid,
        "display_name": f"Ann {cid}",
        "job_title": "CTO",
        "email": "ann@example.com",
        "work_number": "+1 555 0100",
        "lead_score": 40,
        "owner_id": 7,
        "city": "Austin",
        "country": "USA",
        "tags": ["vip"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": updated,
    }
    c.update(kw)
    return c


def _deal(did, updated="2026-01-01T00:00:00Z", **kw):
    d = {
        "id": did,
        "name": f"Deal {did}",
        "amount": "5000.0",
        "deal_stage_id": 1,
        "deal_pipeline_id": 9,
        "probability": 20,
        "owner_id": 7,
        "age": 16,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": updated,
        "fc_widget_collaboration": {"auth_token": "eyJhbGciOiJIUzI1NiJ9.secret.sig"},
    }
    d.update(kw)
    return d


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):  # pragma: no cover - not used by the connector
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("GET", "https://x"), response=self
            )


ENT = {
    "contacts": ("contact", "contact", 101, 102),
    "sales_accounts": ("sales_account", "sales_account", 201, 202),
    "deals": ("deal", "deal", 301, 302),
}


class FakeFreshsales:
    def __init__(self, contacts=(), deals=(), accounts=(), notes=None, page_size=100):
        self.live = {
            "contacts": list(contacts),
            "deals": list(deals),
            "sales_accounts": list(accounts),
        }
        self.recycle = {k: [] for k in self.live}
        self.notes = notes or {}  # (plural, id) -> [note]
        self.calls = []
        self.fail_on = None
        self.page_size = page_size
        self.headers = {}

    def delete(self, plural, rid):
        rec = next(r for r in self.live[plural] if r["id"] == rid)
        self.live[plural].remove(rec)
        self.recycle[plural].append({**rec, "is_deleted": True})

    def _path(self, url):
        return url.split("/crm/sales/api/", 1)[1]

    def post(self, url, json=None, headers=None):
        path = self._path(url)
        self.calls.append(("POST", path, json))
        assert headers["Authorization"] == "Token token=key"
        if self.fail_on and path.startswith(self.fail_on):
            return FakeResponse(500 if self.fail_on == "boom" else 400, {"errors": "x"})
        search = path.split("/")[1]
        plural = next(p for p, v in ENT.items() if v[0] == search)
        rule = json["filter_rule"][0]
        assert rule["attribute"] == "updated_at"
        items = self.live[plural]
        if rule["operator"] == "is_after":
            items = [i for i in items if i["updated_at"] > rule["value"]]  # strict
        else:  # live API: unknown operators silently mean "equals"
            items = [i for i in items if i["updated_at"] == rule["value"]]
        items = sorted(items, key=lambda i: i["updated_at"])
        start = (json["page"] - 1) * json["per_page"]
        page = items[start : start + min(json["per_page"], 100)]
        partial = [{"id": i["id"], "name": i.get("name")} for i in page]  # partial records
        return FakeResponse(200, {plural: partial, "meta": {"total": len(items)}}, self.headers)

    def get(self, url, params=None, headers=None):
        path = self._path(url)
        self.calls.append(("GET", path, params))
        if self.fail_on and path.startswith(self.fail_on):
            return FakeResponse(400, {"errors": "x"})
        parts = path.split("/")
        plural = parts[0]
        _, singular, all_view, recycle_view = ENT[plural]
        if parts[1] == "filters":
            names = {
                "contacts": "All Contacts",
                "sales_accounts": "All Accounts",
                "deals": "All Deals",
            }
            filters = [
                {"id": all_view, "name": names[plural]},
                {"id": recycle_view, "name": "Recycle Bin"},
            ]
            return FakeResponse(200, {"filters": filters})
        if parts[1] == "view":
            items = self.live[plural] if int(parts[2]) == all_view else self.recycle[plural]
            per, page = params["per_page"], params["page"]
            chunk = items[(page - 1) * per : page * per]
            pages = max(1, -(-len(items) // per))
            return FakeResponse(200, {plural: chunk, "meta": {"total_pages": pages}})
        rid = int(parts[1])
        record = next((r for r in self.live[plural] + self.recycle[plural] if r["id"] == rid), None)
        include = (params or {}).get("include", "")
        body = {
            singular: record,
            "users": [USER],
            "deal_stages": STAGES,
            "deal_pipelines": [{"id": 9, "name": "Default Pipeline"}],
        }
        if "notes" in include:
            body["notes"] = self.notes.get((plural, rid), [])
        return FakeResponse(200, body, self.headers)

    def count(self, method, prefix):
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(prefix))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fs.time, "sleep", lambda _s: None)


def _source(fake, **kw):
    kw.setdefault("domain", "acme")
    kw.setdefault("api_key", "key")
    return freshsales_source(client=fake, **kw)


# ---------------------------------------------------------------------------
# DB-free
# ---------------------------------------------------------------------------


def test_base_url_accepts_domain_host_or_url():
    assert base_url("acme") == "https://acme.myfreshworks.com/crm/sales/api"
    assert (
        base_url("acme.myfreshworks.com/crm/sales") == "https://acme.myfreshworks.com/crm/sales/api"
    )
    assert (
        base_url("https://acme.myfreshworks.com") == "https://acme.myfreshworks.com/crm/sales/api"
    )
    assert base_url("acme.freshsales.io") == "https://acme.freshsales.io/api"


def test_since_applies_safety_window_because_is_after_is_strict():
    assert _since("2026-01-01T00:10:00Z", None) == "2026-01-01T00:05:00Z"
    assert _since(None, "2026-01-01T00:10:00+00:00") == "2026-01-01T00:05:00Z"
    assert _since(None, None) is None
    with pytest.raises(ValueError):
        _since(None, "yesterday")


def test_html_notes_become_text():
    assert _html_to_text("<p>Call <b>Ann</b></p><p>tomorrow</p>") == "Call Ann tomorrow"


def test_source_configuration_and_validation(monkeypatch):
    monkeypatch.delenv("FRESHSALES_DOMAIN", raising=False)
    monkeypatch.delenv("FRESHSALES_API_KEY", raising=False)
    source = _source(FakeFreshsales())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == FRESHSALES_SOURCE_NAME
    resource = source.resources[RECORDS_TABLE]
    assert resource.write_disposition == "merge"
    assert resource._hints["columns"]["_deleted"]["hard_delete"] is True
    with pytest.raises(ValueError, match="domain and API key"):
        freshsales_source(client=FakeFreshsales())
    with pytest.raises(ValueError, match="Unknown"):
        _source(FakeFreshsales(), entities=["leads"])


# ---------------------------------------------------------------------------
# dlt pipeline (temp sqlite)
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="fs_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'f.db'}"),
        dataset_name="fs",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, fake, **kw):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(fake, **kw))
    return pipeline


def _rows(pipeline):
    query = f"SELECT id, content FROM {RECORDS_TABLE} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def _state(pipeline):
    return pipeline.state["sources"][FRESHSALES_SOURCE_NAME]["resources"][RECORDS_TABLE]


def test_first_run_reads_full_records_and_builds_safe_documents(tmp_path):
    note = {
        "id": 1,
        "description": "<p>Wants a <b>discount</b></p>",
        "creater_id": 7,
        "created_at": "2026-01-02T00:00:00Z",
    }
    fake = FakeFreshsales([_contact(1)], [_deal(5)], notes={("deals", 5): [note]})
    rows = _rows(_run(tmp_path, fake))
    deal = rows["deals:5"]
    for expected in (
        "Type: Deal",
        "Stage: New",
        "Pipeline: Default Pipeline",
        "Probability: 20%",
        "Owner: Sam Rep",
        "Wants a discount",
        "by Sam Rep",
    ):
        assert expected in deal
    # Allow-list: no chat-widget JWT, no derived/volatile values, no contact details.
    for leaked in ("eyJ", "auth_token", "Age", "16", "rep@example.com"):
        assert leaked not in deal
    contact = rows["contacts:1"]
    assert "Job title: CTO" in contact
    for leaked in ("ann@example.com", "555", "Lead score", "40"):
        assert leaked not in contact
    # partial search results -> one detail call per record
    assert fake.count("GET", "deals/5") == 1


def test_contact_details_kept_when_redaction_off(tmp_path):
    rows = _rows(_run(tmp_path, FakeFreshsales([_contact(1)]), redact_contact_details=False))
    assert "Email: ann@example.com" in rows["contacts:1"]


def test_document_is_tagged_freshsales():
    row = {"id": "deals:5", "title": "Deal 5", "content": "Type: Deal"}
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, content_hash="h", table_name=RECORDS_TABLE),
        uuid5(NAMESPACE_OID, row["id"]),
        FRESHSALES_SOURCE_NAME,
    )
    meta = getattr(item, "system_metadata", None) or item.external_metadata
    assert meta["source"] == FRESHSALES_SOURCE_NAME


def test_second_run_uses_strict_is_after_with_safety_window(tmp_path):
    fake = FakeFreshsales([_contact(1), _contact(2)])
    pipeline = _run(tmp_path, fake)
    cursor = _state(pipeline)["cursors"]["contacts"]
    fake.live["contacts"][0] = _contact(1, updated="2999-01-01T00:00:00Z", job_title="CEO")
    fake.calls.clear()
    rows = _rows(_run(tmp_path, fake))
    search = next(body for m, p, body in fake.calls if p == "filtered_search/contact")
    rule = search["filter_rule"][0]
    assert rule["operator"] == "is_after"
    assert rule["value"] == _since(cursor, None)
    assert "Job title: CEO" in rows["contacts:1"]
    assert fake.count("GET", "contacts/2") == 0  # unchanged -> no detail call


def test_paging_reads_every_changed_record(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_PAGE_SIZE", 2)
    fake = FakeFreshsales([_contact(i, updated=f"2026-01-01T00:00:0{i}Z") for i in range(5)])
    assert len(_rows(_run(tmp_path, fake))) == 5
    assert fake.count("POST", "filtered_search/contact") == 3


def test_deleted_record_is_forgotten_via_recycle_bin(tmp_path):
    fake = FakeFreshsales([_contact(1), _contact(2)], [_deal(5)])
    _run(tmp_path, fake)
    fake.delete("contacts", 1)
    rows = _rows(_run(tmp_path, fake, reconcile=False))
    assert "contacts:1" not in rows and "contacts:2" in rows and "deals:5" in rows


def test_purged_record_is_forgotten_by_reconcile(tmp_path):
    fake = FakeFreshsales([_contact(1), _contact(2)])
    _run(tmp_path, fake)
    fake.live["contacts"] = [c for c in fake.live["contacts"] if c["id"] != 2]  # gone for good
    assert set(_rows(_run(tmp_path, fake))) == {"contacts:1"}


def test_reconcile_off_skips_full_id_listing(tmp_path):
    fake = FakeFreshsales([_contact(1)])
    _run(tmp_path, fake, reconcile=False)
    fake.calls.clear()
    _run(tmp_path, fake, reconcile=False)
    assert fake.count("GET", "contacts/view/101") == 0
    assert fake.count("GET", "contacts/view/102") == 1  # recycle bin still checked


def test_entity_selection(tmp_path):
    fake = FakeFreshsales([_contact(1)], [_deal(5)])
    assert set(_rows(_run(tmp_path, fake, entities=["deals"]))) == {"deals:5"}
    assert fake.count("POST", "filtered_search/contact") == 0


def test_rate_limit_retry_and_minute_budget_pause(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(fs.time, "sleep", slept.append)
    fake = FakeFreshsales([_contact(1)])
    fake.headers = {"per-min-x-ratelimit-remaining": "2"}
    real_post, left = fake.post, [1]

    def flaky(url, json=None, headers=None):
        if left[0]:
            left[0] -= 1
            return FakeResponse(429, headers={"Retry-After": "3"})
        return real_post(url, json, headers)

    fake.post = flaky
    _run(tmp_path, fake)
    assert 3.0 in slept  # honoured Retry-After
    assert 60.0 in slept  # paused when the minute budget was low


def test_failed_run_keeps_cursor_and_memory(tmp_path):
    fake = FakeFreshsales([_contact(1)], [_deal(5)])
    pipeline = _run(tmp_path, fake)
    cursors = dict(_state(pipeline)["cursors"])
    fake.fail_on = "filtered_search/deal"
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)
    pipeline = _pipeline(tmp_path)
    assert _state(pipeline)["cursors"] == cursors
    assert set(_rows(pipeline)) == {"contacts:1", "deals:5"}


def test_bad_key_raises_permission_error(tmp_path):
    fake = FakeFreshsales([_contact(1)])
    fake.post = lambda url, json=None, headers=None: FakeResponse(401)
    with pytest.raises(PipelineStepFailed, match="refused"):
        _run(tmp_path, fake)
