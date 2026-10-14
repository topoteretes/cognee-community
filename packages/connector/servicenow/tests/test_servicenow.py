"""Unit tests for the ServiceNow dlt connector.

Two layers, all runnable without an instance:

* DB-free tests for rendering, paging, retries, the listing/diff sync, journal
  entries, scope changes and deletions, driven through ``sync_records`` with a
  fake ``requests`` session and a plain dict as state.
* dlt-pipeline tests (fake session, temp sqlite destination, ``merge`` like
  ``cognee.remember(..., write_disposition="merge")``) covering the acceptance
  criteria: first sync, edits re-sync, and deleted records are forgotten.
"""

import pytest
import requests

from cognee_community_connector_servicenow.servicenow import (
    SERVICENOW_SOURCE_NAME,
    ServiceNowAPI,
    html_to_text,
    record_to_row,
    servicenow_source,
    sync_records,
)

BASE = "https://dev1.service-now.com"
ONLY_INCIDENTS = {"incident": ""}

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _incident(sys_id, number, summary, updated, **fields):
    return {
        "sys_id": sys_id,
        "number": number,
        "short_description": summary,
        "sys_updated_on": updated,
        "state": "In Progress",
        "priority": "2 - High",
        "assignment_group": "Network",
        "description": f"Details for {number}",
        **fields,
    }


def _article(sys_id, number, title, updated, state="published"):
    return {
        "sys_id": sys_id,
        "number": number,
        "short_description": title,
        "sys_updated_on": updated,
        "workflow_state": state,
        "kb_knowledge_base": "IT",
        "text": "<p>Restart the VPN&nbsp;client.</p><p>Then sign in again.</p>",
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _matches(record, condition):
    if condition.startswith("ORDERBY") or not condition:
        return True
    if "IN" in condition and "=" not in condition:
        field, _, values = condition.partition("IN")
        return str(record.get(field)) in values.split(",")
    field, _, value = condition.partition("=")
    return str(record.get(field)) == value


class FakeServiceNowSession:
    """Stand-in for a ``requests`` session talking to the Table API."""

    def __init__(self, tables, journal=(), page_size=None):
        self.tables = tables  # {table: [record, ...]}
        self.journal = list(journal)  # sys_journal_field rows
        self.page_size = page_size
        self.calls = []  # (table, params)
        self.scripted = []

    def request(self, method, url, params=None, timeout=None):
        table = url.removeprefix(f"{BASE}/api/now/table/")
        self.calls.append((table, dict(params)))
        if self.scripted:
            return self.scripted.pop(0)
        rows = self.journal if table == "sys_journal_field" else self.tables.get(table, [])
        conditions = params.get("sysparm_query", "").split("^")
        rows = [r for r in rows if all(_matches(r, c) for c in conditions)]
        if any(c == "ORDERBYsys_id" for c in conditions):
            rows = sorted(rows, key=lambda r: r["sys_id"])
        offset, limit = params["sysparm_offset"], params["sysparm_limit"]
        if self.page_size:
            limit = min(limit, self.page_size)
        page = rows[offset : offset + limit]
        fields = params.get("sysparm_fields")
        if fields:
            page = [{f: r[f] for f in fields.split(",") if f in r} for r in page]
        return FakeResponse(200, {"result": page})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "cognee_community_connector_servicenow.servicenow.time.sleep", sleeps.append
    )
    return sleeps


def _api(session):
    return ServiceNowAPI(session, BASE)


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_html_to_text_keeps_paragraphs_and_unescapes():
    assert (
        html_to_text("<p>Restart&nbsp;the <b>VPN</b></p><p>a &amp; b</p>")
        == "Restart the VPN\na & b"
    )
    assert html_to_text(None) == ""


def test_incident_document():
    record = _incident(
        "s1", "INC001", "VPN down", "2026-10-01 10:00:00", close_notes="Rebooted gateway"
    )

    row = record_to_row(BASE, "incident", record, ["Work note by ben on 2026-10-01: Checked logs"])

    assert row["id"] == "incident:s1"
    assert row["title"] == "INC001: VPN down"
    assert row["url"] == f"{BASE}/nav_to.do?uri=incident.do?sys_id=s1"
    assert row["content"].splitlines() == [
        "ServiceNow incident INC001. VPN down",
        "State: In Progress",
        "Priority: 2 - High",
        "Assignment group: Network",
        "Description: Details for INC001",
        "Resolution notes: Rebooted gateway",
        "Work note by ben on 2026-10-01: Checked logs",
    ]
    assert "sys_updated_on" not in row["content"]  # volatile fields stay out of the text


def test_knowledge_article_document_renders_html():
    row = record_to_row(BASE, "kb_knowledge", _article("k1", "KB001", "VPN setup", "x"), [])
    assert row["title"] == "KB001: VPN setup"
    assert "Article: Restart the VPN client.\nThen sign in again." in row["content"]


def test_source_declares_document_marker_and_scope():
    from cognee.tasks.ingestion.dlt_utils import (
        PIPELINE_SCOPE_ATTR,
        document_source_tag,
        pipeline_name_for_source,
    )

    source = servicenow_source(instance_url=f"{BASE}/", session=FakeServiceNowSession({}))
    assert SERVICENOW_SOURCE_NAME == "servicenow"
    assert document_source_tag(source) == "servicenow"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == f"servicenow:{BASE}"
    assert pipeline_name_for_source(source, "a") != pipeline_name_for_source(source, "b")


def test_source_validates_configuration(monkeypatch):
    monkeypatch.delenv("SERVICENOW_INSTANCE_URL", raising=False)
    with pytest.raises(ValueError, match="instance URL required"):
        servicenow_source(session=FakeServiceNowSession({}))
    with pytest.raises(ValueError, match="Unsupported tables"):
        servicenow_source(
            instance_url=BASE, tables={"sys_user": ""}, session=FakeServiceNowSession({})
        )


# ---------------------------------------------------------------------------
# HTTP (DB-free)
# ---------------------------------------------------------------------------


def test_table_pages_by_offset():
    rows = [_incident(f"s{i}", f"INC{i}", "x", "t") for i in range(5)]
    session = FakeServiceNowSession({"incident": rows})

    got = list(_api(session).table("incident", {"sysparm_query": "ORDERBYsys_id"}, page_size=2))

    assert [r["sys_id"] for r in got] == ["s0", "s1", "s2", "s3", "s4"]
    assert [c[1]["sysparm_offset"] for c in session.calls] == [0, 2, 4]


def test_rate_limit_is_retried_and_auth_errors_are_not(_no_sleep):
    session = FakeServiceNowSession({"incident": []})
    session.scripted = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(503)]
    assert list(_api(session).table("incident", {})) == []
    assert _no_sleep == [3.0, 2.0]

    session.scripted = [FakeResponse(401)]
    with pytest.raises(requests.HTTPError):
        list(_api(session).table("incident", {}))
    assert _no_sleep == [3.0, 2.0]  # no extra retry for 401


# ---------------------------------------------------------------------------
# Sync logic (DB-free)
# ---------------------------------------------------------------------------


def test_first_sync_fetches_everything_with_display_values():
    session = FakeServiceNowSession(
        {
            "incident": [
                _incident("s1", "INC001", "VPN down", "t1"),
                _incident("s2", "INC002", "Mail", "t1"),
            ]
        }
    )
    state = {}

    rows = list(sync_records(_api(session), ONLY_INCIDENTS, False, state))

    assert [r["id"] for r in rows] == ["incident:s1", "incident:s2"]
    listing, fetch = session.calls
    assert listing[1]["sysparm_fields"] == "sys_id,sys_updated_on"  # cheap listing first
    assert fetch[1]["sysparm_query"] == "sys_idINs1,s2"
    assert fetch[1]["sysparm_display_value"] == "true"
    assert state == {"tables": {"incident": {"s1": "t1", "s2": "t1"}}}


def test_only_changed_records_are_fetched_again():
    incidents = [
        _incident("s1", "INC001", "VPN down", "t1"),
        _incident("s2", "INC002", "Mail", "t1"),
    ]
    session = FakeServiceNowSession({"incident": incidents})
    state = {}
    list(sync_records(_api(session), ONLY_INCIDENTS, False, state))

    incidents[1].update(sys_updated_on="t2", close_notes="Fixed DNS")
    session.calls.clear()
    rows = list(sync_records(_api(session), ONLY_INCIDENTS, False, state))

    assert [r["id"] for r in rows] == ["incident:s2"]
    assert "Resolution notes: Fixed DNS" in rows[0]["content"]
    assert session.calls[1][1]["sysparm_query"] == "sys_idINs2"


def test_deleted_or_out_of_scope_records_are_tombstoned():
    articles = [_article("k1", "KB001", "VPN", "t1"), _article("k2", "KB002", "SSO", "t1")]
    incidents = [_incident("s1", "INC001", "VPN down", "t1")]
    session = FakeServiceNowSession({"kb_knowledge": articles, "incident": incidents})
    scope = {"kb_knowledge": "workflow_state=published", "incident": ""}
    state = {}
    list(sync_records(_api(session), scope, False, state))

    articles[0]["workflow_state"] = "retired"  # leaves the published scope
    incidents.clear()  # deleted upstream
    rows = list(sync_records(_api(session), scope, False, state))

    assert {"id": "kb_knowledge:k1", "_deleted": True} in rows
    assert {"id": "incident:s1", "_deleted": True} in rows
    assert state["tables"] == {"kb_knowledge": {"k2": "t1"}, "incident": {}}


def test_removed_table_forgets_its_records():
    session = FakeServiceNowSession({"incident": [_incident("s1", "INC001", "VPN", "t1")]})
    state = {}
    list(sync_records(_api(session), ONLY_INCIDENTS, False, state))

    rows = list(sync_records(_api(session), {"problem": ""}, False, state))

    assert {"id": "incident:s1", "_deleted": True} in rows
    assert "incident" not in state["tables"]


def test_journal_entries_are_opt_in_and_attached_in_order():
    journal = [
        {
            "name": "incident",
            "element": "work_notes",
            "element_id": "s1",
            "value": "Checked logs",
            "sys_created_by": "ben",
            "sys_created_on": "2026-10-01 10:00:00",
        },
        {
            "name": "incident",
            "element": "comments",
            "element_id": "s1",
            "value": "<p>Fixed?</p>",
            "sys_created_by": "asha",
            "sys_created_on": "2026-10-01 11:00:00",
        },
    ]
    session = FakeServiceNowSession({"incident": [_incident("s1", "INC001", "VPN", "t1")]}, journal)

    without = list(sync_records(_api(session), ONLY_INCIDENTS, False, {}))
    assert not any(c[0] == "sys_journal_field" for c in session.calls)
    assert "Work note" not in without[0]["content"]

    rows = list(sync_records(_api(session), ONLY_INCIDENTS, True, {}))
    assert rows[0]["content"].splitlines()[-2:] == [
        "Work note by ben on 2026-10-01 10:00:00: Checked logs",
        "Comment by asha on 2026-10-01 11:00:00: Fixed?",
    ]


def test_failed_listing_aborts_before_any_tombstone():
    session = FakeServiceNowSession({"incident": []})
    session.scripted = [FakeResponse(403)]
    state = {"tables": {"incident": {"s1": "t1"}}}
    with pytest.raises(requests.HTTPError):
        list(sync_records(_api(session), ONLY_INCIDENTS, False, state))
    assert state == {"tables": {"incident": {"s1": "t1"}}}


def test_fetch_is_batched():
    rows = [_incident(f"s{i:03}", f"INC{i}", "x", "t") for i in range(250)]
    session = FakeServiceNowSession({"incident": rows})

    synced = list(sync_records(_api(session), ONLY_INCIDENTS, False, {}))

    assert len(synced) == 250
    fetches = [c for c in session.calls if c[1]["sysparm_query"].startswith("sys_idIN")]
    assert [len(c[1]["sysparm_query"].split(",")) for c in fetches] == [100, 100, 50]


# ---------------------------------------------------------------------------
# dlt pipeline: merge + _deleted hard delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, session, **kwargs):
    """Run servicenow_source the way cognee.remember(write_disposition="merge") does."""
    db_path = (tmp_path / "servicenow.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="servicenow_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="servicenow_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    source = servicenow_source(instance_url=BASE, session=session, **kwargs)
    pipeline.run(source, write_disposition="merge", primary_key="id")
    return pipeline


def _read(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM servicenow_records") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def _instance():
    return FakeServiceNowSession(
        {
            "kb_knowledge": [_article("k1", "KB001", "VPN setup", "t1")],
            "incident": [
                _incident("s1", "INC001", "VPN down", "t1"),
                _incident("s2", "INC002", "Mail", "t1"),
            ],
        }
    )


def test_first_sync_loads_articles_and_incidents(dlt_mod, tmp_path):
    pipeline = _run_sync(dlt_mod, tmp_path, _instance())
    records = _read(pipeline)
    assert set(records) == {"kb_knowledge:k1", "incident:s1", "incident:s2"}
    assert "Restart the VPN client." in records["kb_knowledge:k1"]


def test_edit_is_reflected_and_unchanged_records_survive(dlt_mod, tmp_path):
    session = _instance()
    _run_sync(dlt_mod, tmp_path, session)

    session.tables["incident"][1].update(sys_updated_on="t2", close_notes="Fixed DNS")
    pipeline = _run_sync(dlt_mod, tmp_path, session)

    records = _read(pipeline)
    assert "Fixed DNS" in records["incident:s2"]
    assert set(records) == {"kb_knowledge:k1", "incident:s1", "incident:s2"}


def test_deleted_record_and_retired_article_are_removed(dlt_mod, tmp_path):
    session = _instance()
    _run_sync(dlt_mod, tmp_path, session)

    session.tables["incident"].pop(0)
    session.tables["kb_knowledge"][0]["workflow_state"] = "retired"
    pipeline = _run_sync(dlt_mod, tmp_path, session)

    # Tombstoned rows are hard-deleted by the merge, so cognee's read-back no
    # longer sees them and orphan cleanup forgets them.
    assert set(_read(pipeline)) == {"incident:s2"}
