"""Unit tests for the SharePoint / OneDrive connector.

Microsoft Graph is fully mocked via ``FakeGraphSession`` — no msal, no network
and no live tenant are required, so these run in CI. Coverage:

  - initial sync enumerates a library and stores its delta link
  - row ids are scoped by drive, so two libraries cannot collide
  - unsupported extensions and oversized files are skipped with a visible reason
  - an unreadable file is skipped without failing the sync
  - incremental re-sync yields nothing when nothing changed, one row when a file
    is modified, and a hard-delete marker when a file is removed
  - an invalidated delta link (HTTP 410) triggers a full re-enumeration
  - a Graph failure raises and leaves the cursor untouched
  - an Entra token error surfaces its own message
  - the dlt resource is wired with merge + id PK + the hard_delete column
"""

import io

import pytest

from cognee_community_connector_sharepoint import sharepoint as sp
from cognee_community_connector_sharepoint.sharepoint import (
    GRAPH_BASE_URL,
    _SharePointConfig,
    sharepoint_source,
)

SITE_ID = "contoso.sharepoint.com,site-guid,web-guid"
DRIVE_ID = "drive-1"
OTHER_DRIVE_ID = "drive-2"


def _delta_url(drive_id=DRIVE_ID):
    return f"{GRAPH_BASE_URL}/drives/{drive_id}/root/delta"


class _FakeHTTPError(Exception):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPError(self)


class FakeGraphSession:
    """Stands in for the authenticated ``requests`` session.

    ``responses`` maps a URL to a payload dict, an int status code, or a list of
    either that is consumed one entry per call.
    """

    def __init__(self, *, drives_by_site=None, responses=None, contents=None):
        self.drives_by_site = drives_by_site or {}
        self.responses = responses or {}
        self.contents = contents or {}
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)

        if url.endswith("/drives"):
            site_id = url.split("/sites/")[1].rsplit("/drives", 1)[0]
            drive_ids = self.drives_by_site[site_id]
            return _FakeResponse(payload={"value": [{"id": d} for d in drive_ids]})

        if url.endswith("/content"):
            item_id = url.split("/items/")[1].rsplit("/content", 1)[0]
            if item_id not in self.contents:
                return _FakeResponse(status_code=404)
            return _FakeResponse(content=self.contents[item_id])

        if url in self.responses:
            entry = self.responses[url]
            if isinstance(entry, list):
                entry = entry.pop(0)
            if isinstance(entry, int):
                return _FakeResponse(status_code=entry)
            return _FakeResponse(payload=entry)

        raise AssertionError(f"unexpected Graph URL: {url}")


def _config(**overrides):
    defaults = {"site_id": SITE_ID, "drive_ids": (), "max_file_size_mb": 25}
    defaults.update(overrides)
    return _SharePointConfig(**defaults)


def _file(item_id, name, size=10, **extra):
    return {
        "id": item_id,
        "name": name,
        "file": {},
        "size": size,
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        **extra,
    }


def _session(delta_pages, *, contents=None, drive_id=DRIVE_ID, drives_by_site=None):
    return FakeGraphSession(
        drives_by_site=drives_by_site or {SITE_ID: [drive_id]},
        responses={_delta_url(drive_id): delta_pages},
        contents=contents if contents is not None else {},
    )


@pytest.fixture
def warnings(monkeypatch):
    """Capture the connector's own warnings without depending on log config."""
    captured = []

    class _Logger:
        def warning(self, message, *args):
            captured.append(message % args if args else message)

        def info(self, *args, **kwargs):
            pass

    monkeypatch.setattr(sp, "logger", _Logger())
    return captured


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sp.time, "sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# Ingest path
# ---------------------------------------------------------------------------
def test_initial_sync_yields_rows_and_stores_delta_link():
    session = _session(
        {
            "value": [_file("a", "notes.txt"), _file("b", "data.csv")],
            "@odata.deltaLink": "link-1",
        },
        contents={"a": b"hello notes", "b": b"col1,col2"},
    )
    state = {}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [row["title"] for row in rows] == ["notes.txt", "data.csv"]
    assert [row["content"] for row in rows] == ["hello notes", "col1,col2"]
    assert all(row["_deleted"] is False for row in rows)
    assert rows[0]["url"] == "https://contoso.sharepoint.com/notes.txt"
    assert state["delta_links"] == {DRIVE_ID: "link-1"}


def test_row_id_is_scoped_by_drive():
    session = _session(
        {"value": [_file("shared-id", "a.md")], "@odata.deltaLink": "link-1"},
        contents={"shared-id": b"# heading"},
    )

    rows = list(sp._iter_rows(session, _config(), {}))

    assert rows[0]["id"] == f"{DRIVE_ID}:shared-id"


def test_delta_paginates_until_the_delta_link():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={
            _delta_url(): {
                "value": [_file("a", "one.txt")],
                "@odata.nextLink": "https://graph.example/page-2",
            },
            "https://graph.example/page-2": {
                "value": [_file("b", "two.txt")],
                "@odata.deltaLink": "link-1",
            },
        },
        contents={"a": b"one", "b": b"two"},
    )
    state = {}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [row["title"] for row in rows] == ["one.txt", "two.txt"]
    assert state["delta_links"] == {DRIVE_ID: "link-1"}


def test_folders_are_ignored():
    session = _session(
        {
            "value": [
                {"id": "f1", "name": "Reports", "folder": {}},
                _file("a", "report.txt"),
            ],
            "@odata.deltaLink": "link-1",
        },
        contents={"a": b"body"},
    )

    rows = list(sp._iter_rows(session, _config(), {}))

    assert [row["title"] for row in rows] == ["report.txt"]


def test_replayed_item_uses_the_last_occurrence():
    session = _session(
        {
            "value": [_file("a", "old-name.txt"), _file("a", "new-name.txt")],
            "@odata.deltaLink": "link-1",
        },
        contents={"a": b"body"},
    )

    rows = list(sp._iter_rows(session, _config(), {}))

    assert [row["title"] for row in rows] == ["new-name.txt"]


def test_multiple_libraries_track_their_own_delta_links():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID, OTHER_DRIVE_ID]},
        responses={
            _delta_url(DRIVE_ID): {"value": [_file("a", "a.txt")], "@odata.deltaLink": "link-a"},
            _delta_url(OTHER_DRIVE_ID): {
                "value": [_file("b", "b.txt")],
                "@odata.deltaLink": "link-b",
            },
        },
        contents={"a": b"aaa", "b": b"bbb"},
    )
    state = {}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [row["id"] for row in rows] == [f"{DRIVE_ID}:a", f"{OTHER_DRIVE_ID}:b"]
    assert state["delta_links"] == {DRIVE_ID: "link-a", OTHER_DRIVE_ID: "link-b"}


def test_explicit_drive_ids_skip_the_site_lookup():
    session = _session(
        {"value": [_file("a", "a.txt")], "@odata.deltaLink": "link-1"},
        contents={"a": b"aaa"},
        drives_by_site={},
    )

    rows = list(sp._iter_rows(session, _config(site_id=None, drive_ids=(DRIVE_ID,)), {}))

    assert len(rows) == 1
    assert not any(call.endswith("/drives") for call in session.calls)


# ---------------------------------------------------------------------------
# Skip-with-a-reason
# ---------------------------------------------------------------------------
def test_unsupported_extension_is_skipped_with_a_reason(warnings):
    session = _session(
        {
            "value": [_file("a", "deck.pptx"), _file("b", "readme.md")],
            "@odata.deltaLink": "link-1",
        },
        contents={"a": b"binary", "b": b"# readme"},
    )

    rows = list(sp._iter_rows(session, _config(), {}))

    assert [row["title"] for row in rows] == ["readme.md"]
    assert any("deck.pptx" in message and "not one of" in message for message in warnings)


def test_oversized_file_is_skipped_with_a_reason(warnings):
    session = _session(
        {
            "value": [_file("a", "huge.pdf", size=30 * 1024 * 1024), _file("b", "small.txt")],
            "@odata.deltaLink": "link-1",
        },
        contents={"b": b"small"},
    )

    rows = list(sp._iter_rows(session, _config(max_file_size_mb=25), {}))

    assert [row["title"] for row in rows] == ["small.txt"]
    assert any("huge.pdf" in message and "max_file_size_mb" in message for message in warnings)


def test_unreadable_file_is_skipped_without_failing_the_sync(warnings):
    # "a" has no content entry, so the download 404s mid-sync.
    session = _session(
        {
            "value": [_file("a", "gone.txt"), _file("b", "fine.txt")],
            "@odata.deltaLink": "link-1",
        },
        contents={"b": b"fine"},
    )
    state = {}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [row["title"] for row in rows] == ["fine.txt"]
    assert any("gone.txt" in message for message in warnings)
    assert state["delta_links"] == {DRIVE_ID: "link-1"}


def test_empty_file_produces_no_row():
    session = _session(
        {"value": [_file("a", "blank.txt")], "@odata.deltaLink": "link-1"},
        contents={"a": b"   \n  "},
    )

    assert list(sp._iter_rows(session, _config(), {})) == []


# ---------------------------------------------------------------------------
# Incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
def test_incremental_sync_with_no_changes_yields_nothing():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={"link-1": {"value": [], "@odata.deltaLink": "link-2"}},
    )
    state = {"delta_links": {DRIVE_ID: "link-1"}}

    assert list(sp._iter_rows(session, _config(), state)) == []
    assert state["delta_links"] == {DRIVE_ID: "link-2"}


def test_modified_file_yields_one_row():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={"link-1": {"value": [_file("a", "notes.txt")], "@odata.deltaLink": "link-2"}},
        contents={"a": b"edited"},
    )
    state = {"delta_links": {DRIVE_ID: "link-1"}}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [(row["id"], row["content"]) for row in rows] == [(f"{DRIVE_ID}:a", "edited")]
    assert state["delta_links"] == {DRIVE_ID: "link-2"}


def test_deleted_item_yields_hard_delete_tombstone():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={"link-1": {"value": [{"id": "a", "deleted": {}}], "@odata.deltaLink": "link-2"}},
    )
    state = {"delta_links": {DRIVE_ID: "link-1"}}

    rows = list(sp._iter_rows(session, _config(), state))

    assert rows == [{"id": f"{DRIVE_ID}:a", "_deleted": True}]
    # A tombstone carries no name or size, so nothing is downloaded for it.
    assert not any(call.endswith("/content") for call in session.calls)


# ---------------------------------------------------------------------------
# Delta link invalidation and failure handling
# ---------------------------------------------------------------------------
def test_expired_delta_link_triggers_a_full_resync(warnings):
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={
            "link-1": 410,
            _delta_url(): {"value": [_file("a", "notes.txt")], "@odata.deltaLink": "link-9"},
        },
        contents={"a": b"re-read"},
    )
    state = {"delta_links": {DRIVE_ID: "link-1"}}

    rows = list(sp._iter_rows(session, _config(), state))

    assert [row["content"] for row in rows] == ["re-read"]
    assert state["delta_links"] == {DRIVE_ID: "link-9"}
    assert any("invalidated" in message for message in warnings)


def test_410_on_the_first_enumeration_is_not_swallowed():
    session = FakeGraphSession(drives_by_site={SITE_ID: [DRIVE_ID]}, responses={_delta_url(): 410})
    state = {}

    with pytest.raises(Exception, match="410"):
        list(sp._iter_rows(session, _config(), state))


def test_graph_error_raises_and_leaves_the_cursor_untouched():
    session = FakeGraphSession(drives_by_site={SITE_ID: [DRIVE_ID]}, responses={"link-1": 403})
    state = {"delta_links": {DRIVE_ID: "link-1"}}

    with pytest.raises(Exception, match="403"):
        list(sp._iter_rows(session, _config(), state))

    assert state["delta_links"] == {DRIVE_ID: "link-1"}


def test_throttled_request_is_retried():
    session = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={
            _delta_url(): [429, {"value": [_file("a", "a.txt")], "@odata.deltaLink": "link-1"}]
        },
        contents={"a": b"body"},
    )

    rows = list(sp._iter_rows(session, _config(), {}))

    assert len(rows) == 1


def test_site_listing_failure_is_reported_with_the_site_id():
    session = FakeGraphSession(drives_by_site={})

    with pytest.raises(RuntimeError, match=SITE_ID):
        list(sp._iter_rows(session, _config(), {}))


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------
def test_is_supported_file():
    assert sp.is_supported_file("a.PDF")
    assert sp.is_supported_file("a.docx")
    assert sp.is_supported_file("a.xlsx")
    assert sp.is_supported_file("a.md")
    assert not sp.is_supported_file("a.pptx")
    assert not sp.is_supported_file("a.doc")
    assert not sp.is_supported_file("noextension")


def test_docx_text_is_extracted():
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_paragraph("First paragraph.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "left"
    table.rows[0].cells[1].text = "right"
    buffer = io.BytesIO()
    document.save(buffer)

    text = sp._extract_docx_text(buffer.getvalue())

    assert "First paragraph." in text
    assert "left, right" in text


def test_xlsx_text_is_extracted():
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Budget"
    sheet.append(["item", "cost"])
    sheet.append(["laptop", 1200])
    buffer = io.BytesIO()
    workbook.save(buffer)

    text = sp._extract_xlsx_text(buffer.getvalue())

    assert "# Budget" in text
    assert "item, cost" in text
    assert "laptop, 1200" in text


def test_pdf_extraction_does_not_raise():
    pypdf = pytest.importorskip("pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    assert isinstance(sp._extract_pdf_text(buffer.getvalue()), str)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_acquire_token_surfaces_the_entra_error_message():
    class _App:
        def acquire_token_for_client(self, scopes):
            return {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            }

    with pytest.raises(ValueError, match="Invalid client secret"):
        sp.acquire_token(_App())


def test_acquire_token_returns_the_access_token():
    class _App:
        def acquire_token_for_client(self, scopes):
            assert scopes == [sp.GRAPH_DEFAULT_SCOPE]
            return {"access_token": "token-value"}

    assert sp.acquire_token(_App()) == "token-value"


def test_build_graph_session_requires_credentials(monkeypatch):
    pytest.importorskip("msal")
    for name in ("MICROSOFT_TENANT_ID", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="MICROSOFT_TENANT_ID"):
        sp.build_graph_session()


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------
def test_sharepoint_source_requires_site_or_drive_ids(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.delenv("SHAREPOINT_DRIVE_IDS", raising=False)

    with pytest.raises(ValueError, match="site_id or drive_ids"):
        sharepoint_source()


def test_sharepoint_source_reads_drive_ids_from_the_environment(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.setenv("SHAREPOINT_DRIVE_IDS", " drive-a , drive-b ")

    resource = sharepoint_source(session=FakeGraphSession())

    assert resource.name == "sharepoint_files"


def test_sharepoint_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"cognee\[dlt\]"):
        sharepoint_source(site_id=SITE_ID)


def test_sharepoint_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = sharepoint_source(site_id=SITE_ID, session=FakeGraphSession())
    assert resource.name == "sharepoint_files"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_sharepoint_source_declares_the_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = sharepoint_source(site_id=SITE_ID, session=FakeGraphSession())

    assert document_source_tag(resource) == "sharepoint"


def test_split_ids_parses_and_trims_a_comma_separated_list():
    assert sp._split_ids(" drive-a , drive-b ") == ["drive-a", "drive-b"]
    assert sp._split_ids("") == []
    assert sp._split_ids(None) == []


# ---------------------------------------------------------------------------
# End-to-end through a real dlt merge (no LLM, no network)
# ---------------------------------------------------------------------------
def test_e2e_dlt_merge_persists_the_delta_link_and_hard_deletes(tmp_path):
    """Two real dlt runs: the delta link survives in resource state between them,
    and a hard-delete marker physically removes the row from the destination."""
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_sharepoint_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "sharepoint.duckdb")),
        dataset_name="library",
    )

    # Run 1: a tokenless delta enumerates two files and returns "link-1".
    session1 = _session(
        {
            "value": [_file("a", "keep.txt"), _file("b", "drop.txt")],
            "@odata.deltaLink": "link-1",
        },
        contents={"a": b"keep me", "b": b"drop me"},
    )
    pipeline.run(sharepoint_source(site_id=SITE_ID, session=session1))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM sharepoint_files")[0][0] == 2

    # Run 2: the connector must follow "link-1" rather than re-enumerating, which
    # only works if dlt persisted the nested delta_links dict from run 1.
    session2 = FakeGraphSession(
        drives_by_site={SITE_ID: [DRIVE_ID]},
        responses={"link-1": {"value": [{"id": "b", "deleted": {}}], "@odata.deltaLink": "link-2"}},
    )
    pipeline.run(sharepoint_source(site_id=SITE_ID, session=session2))

    assert "link-1" in session2.calls
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM sharepoint_files")
    assert [row[0] for row in rows] == [f"{DRIVE_ID}:a"]


def test_build_graph_session_reports_an_unresolvable_tenant(monkeypatch):
    import sys
    import types

    # msal performs OIDC discovery in the constructor and raises a raw ValueError
    # for a bad tenant; the connector must restate it in its own terms.
    fake_msal = types.ModuleType("msal")

    def _fail(*args, **kwargs):
        raise ValueError("Unable to get authority configuration for ...")

    fake_msal.ConfidentialClientApplication = _fail
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "not-a-tenant")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "secret")

    with pytest.raises(ValueError, match="could not resolve tenant"):
        sp.build_graph_session()
