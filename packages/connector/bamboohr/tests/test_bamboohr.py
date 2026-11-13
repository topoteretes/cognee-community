"""Unit tests for the BambooHR connector.

The BambooHR REST API is mocked via ``FakeSession`` — no network traffic and
no live credentials are required, so these run in CI.
"""

import io

import pytest
import requests

from cognee_community_connector_bamboohr import bamboohr
from cognee_community_connector_bamboohr.bamboohr import (
    DEFAULT_EMPLOYEE_FIELDS,
    DOCUMENT_SOURCE_ATTR,
    EMPLOYEES_TABLE_NAME,
    FILES_TABLE_NAME,
    _base_url,
    _employee_to_row,
    _get_changed,
    _make_session,
    _request,
    bamboohr_source,
    sync_employees,
    sync_files,
)

BASE_URL = _base_url("acme")


# ---------------------------------------------------------------------------
# Fake BambooHR REST API
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None, content=b""):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns queued responses in order and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries back off with time.sleep; make that instant in tests."""
    monkeypatch.setattr(bamboohr.time, "sleep", lambda _seconds: None)


# Response shape copied from BambooHR's "Get Changed Employee IDs" docs.
CHANGED_BODY = {
    "latest": "2026-06-11T16:07:32.000Z",
    "employees": {
        "123": {"id": "123", "action": "Updated", "lastChanged": "2026-06-11T16:07:32.000Z"},
        "456": {"id": "456", "action": "Inserted", "lastChanged": "2026-06-10T14:22:15.000Z"},
    },
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def test_base_url_uses_company_subdomain():
    assert _base_url("acme") == "https://acme.bamboohr.com/api/v1"


def test_session_uses_api_key_basic_auth_and_asks_for_json():
    session = _make_session("secret-key")
    assert session.auth == ("secret-key", "x")
    assert session.headers["Accept"] == "application/json"


def test_get_changed_sends_since_and_returns_body():
    session = FakeSession([FakeResponse(json_body=CHANGED_BODY)])

    body = _get_changed(session, BASE_URL, "2026-01-01T00:00:00Z")

    assert body == CHANGED_BODY
    assert session.calls == [(f"{BASE_URL}/employees/changed", {"since": "2026-01-01T00:00:00Z"})]


def test_request_retries_rate_limit_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "1"}),
            FakeResponse(503),
            FakeResponse(json_body=CHANGED_BODY),
        ]
    )

    response = _request(session, f"{BASE_URL}/employees/changed")

    assert response.status_code == 200
    assert len(session.calls) == 3


def test_request_retries_network_errors():
    session = FakeSession([requests.ConnectionError("reset"), FakeResponse(json_body={})])

    assert _request(session, BASE_URL).status_code == 200


def test_request_does_not_retry_permanent_errors():
    # A 401 (bad API key) will not fix itself, so it is returned immediately.
    session = FakeSession([FakeResponse(401)])

    assert _request(session, BASE_URL).status_code == 401
    assert len(session.calls) == 1


def test_get_changed_raises_on_auth_failure():
    session = FakeSession([FakeResponse(401)])

    with pytest.raises(requests.HTTPError):
        _get_changed(session, BASE_URL, "2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
class FakeBambooHR:
    """Routes requests like the real API: change feed, employees and files.

    ``changes`` maps employee id → action. ``employees`` maps id → record; an
    id missing from it answers 404, like an employee that no longer exists.
    Ids in ``failing`` answer 500 on every attempt (a persistent outage).
    ``files`` maps file id → listing entry plus ``_data`` (the download body;
    ``None`` answers 404).
    """

    def __init__(
        self,
        changes=None,
        employees=None,
        latest="2026-06-11T16:07:32.000Z",
        failing=(),
        files=None,
    ):
        self.changes = changes or {}
        self.employees = employees or {}
        self.latest = latest
        self.failing = set(failing)
        self.files = files or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url == f"{BASE_URL}/files/view":
            # Shape from BambooHR's "List Company Files" docs.
            categories = {}
            for file in self.files.values():
                entry = {k: v for k, v in file.items() if not k.startswith("_")}
                categories.setdefault(file.get("_category", "Policies"), []).append(entry)
            body = {"categories": [{"name": n, "files": f} for n, f in categories.items()]}
            return FakeResponse(json_body=body)
        if url.startswith(f"{BASE_URL}/files/"):
            file = self.files.get(int(url.rsplit("/", 1)[1]))
            if file is None or file["_data"] is None:
                return FakeResponse(404)
            return FakeResponse(content=file["_data"])
        if url == f"{BASE_URL}/employees/changed":
            employees = {
                eid: {"id": eid, "action": action, "lastChanged": self.latest}
                for eid, action in self.changes.items()
            }
            # Mirror the API returning [] (not {}) when nothing changed.
            return FakeResponse(json_body={"latest": self.latest, "employees": employees or []})
        employee_id = url.rsplit("/", 1)[1]
        if employee_id in self.failing:
            return FakeResponse(500)
        if employee_id not in self.employees:
            return FakeResponse(404)
        return FakeResponse(json_body=self.employees[employee_id])


def _employee(eid, first="Jane", last="Doe", status="Active", **extra):
    return {"id": eid, "firstName": first, "lastName": last, "status": status, **extra}


def test_employee_row_renders_only_allowlisted_fields():
    employee = _employee("7", jobTitle="Engineer", department="R&D", ssn="000-00-0000")

    row = _employee_to_row(employee, ["firstName", "lastName", "jobTitle", "department"])

    assert row == {
        "id": "employee:7",
        "title": "Jane Doe — Engineer",
        "content": "firstName: Jane\nlastName: Doe\njobTitle: Engineer\ndepartment: R&D",
        "_deleted": False,
    }


def test_employee_row_prefers_preferred_name_and_skips_empty_values():
    employee = _employee("7", preferredName="JJ", department="")

    row = _employee_to_row(employee, ["preferredName", "lastName", "department"])

    assert row["title"] == "JJ Doe"
    assert "department" not in row["content"]


def test_first_sync_starts_from_epoch_and_saves_cursor():
    api = FakeBambooHR(
        {"1": "Inserted", "2": "Inserted"}, {"1": _employee("1"), "2": _employee("2")}
    )
    state = {}

    rows = list(sync_employees(api, BASE_URL, state))

    assert [row["id"] for row in rows] == ["employee:1", "employee:2"]
    assert api.calls[0][1] == {"since": "1970-01-01T00:00:00Z"}
    assert state["since"] == "2026-06-11T16:07:32.000Z"


def test_incremental_sync_uses_saved_cursor():
    api = FakeBambooHR({"1": "Updated"}, {"1": _employee("1")}, latest="2026-07-01T00:00:00.000Z")
    state = {"since": "2026-06-11T16:07:32.000Z"}

    list(sync_employees(api, BASE_URL, state))

    assert api.calls[0][1] == {"since": "2026-06-11T16:07:32.000Z"}
    assert state["since"] == "2026-07-01T00:00:00.000Z"


def test_only_allowlisted_fields_are_requested_plus_status():
    api = FakeBambooHR({"1": "Inserted"}, {"1": _employee("1")})

    list(sync_employees(api, BASE_URL, {}, fields=["firstName", "jobTitle"]))

    assert api.calls[1] == (f"{BASE_URL}/employees/1", {"fields": "firstName,jobTitle,status"})


def test_no_changes_yields_nothing_and_keeps_cursor():
    api = FakeBambooHR({}, {}, latest=None)
    state = {"since": "2026-06-11T16:07:32.000Z"}

    assert list(sync_employees(api, BASE_URL, state)) == []
    assert state["since"] == "2026-06-11T16:07:32.000Z"


def test_deleted_and_missing_employees_become_tombstones():
    # "3" is reported Deleted; "4" is reported Updated but answers 404.
    api = FakeBambooHR({"3": "Deleted", "4": "Updated"}, {})

    rows = list(sync_employees(api, BASE_URL, {}))

    assert rows == [
        {"id": "employee:3", "_deleted": True},
        {"id": "employee:4", "_deleted": True},
    ]
    # A Deleted action needs no per-employee lookup.
    assert f"{BASE_URL}/employees/3" not in [url for url, _ in api.calls]


def test_inactive_employees_are_forgotten_by_default():
    api = FakeBambooHR({"5": "Updated"}, {"5": _employee("5", status="Inactive")})

    assert list(sync_employees(api, BASE_URL, {})) == [{"id": "employee:5", "_deleted": True}]


def test_inactive_employees_are_kept_when_requested():
    api = FakeBambooHR({"5": "Updated"}, {"5": _employee("5", status="Inactive")})

    rows = list(sync_employees(api, BASE_URL, {}, include_inactive=True))

    assert rows[0]["_deleted"] is False
    assert "status: Inactive" in rows[0]["content"]


def test_failed_lookup_aborts_and_does_not_advance_cursor():
    api = FakeBambooHR({"1": "Inserted"}, {"1": _employee("1")}, failing={"1"})
    state = {"since": "2026-06-11T16:07:32.000Z"}

    with pytest.raises(requests.HTTPError):
        list(sync_employees(api, BASE_URL, state))
    assert state["since"] == "2026-06-11T16:07:32.000Z"


# ---------------------------------------------------------------------------
# Source wiring
# ---------------------------------------------------------------------------
def test_source_requires_company_domain(monkeypatch):
    monkeypatch.delenv("BAMBOOHR_COMPANY_DOMAIN", raising=False)

    with pytest.raises(ValueError, match="company domain"):
        bamboohr_source(api_key="k")


def test_source_requires_api_key(monkeypatch):
    monkeypatch.delenv("BAMBOOHR_API_KEY", raising=False)

    with pytest.raises(ValueError, match="API key"):
        bamboohr_source(company_domain="acme")


def test_source_reads_credentials_from_env(monkeypatch):
    monkeypatch.setenv("BAMBOOHR_COMPANY_DOMAIN", "acme")
    monkeypatch.setenv("BAMBOOHR_API_KEY", "k")

    assert bamboohr_source() is not None


def test_source_opts_into_document_mode_with_merge_and_hard_delete():
    source = bamboohr_source(company_domain="acme", session=FakeBambooHR({}, {}))
    resource = source.resources[EMPLOYEES_TABLE_NAME]

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "bamboohr"
    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True


def test_default_fields_exclude_sensitive_data():
    for sensitive in ("ssn", "dateOfBirth", "payRate", "address1", "homePhone", "gender"):
        assert sensitive not in DEFAULT_EMPLOYEE_FIELDS


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="test_bamboohr_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'bamboohr.db'}"),
        dataset_name="hr",
    )

    def stored_ids():
        with pipeline.sql_client() as client:
            rows = client.execute_sql(f"SELECT id FROM {EMPLOYEES_TABLE_NAME} ORDER BY id")
        return [row[0] for row in rows]

    # Sync #1: two employees land in staging.
    api = FakeBambooHR(
        {"1": "Inserted", "2": "Inserted"}, {"1": _employee("1"), "2": _employee("2")}
    )
    pipeline.run(bamboohr_source(company_domain="acme", session=api))
    assert stored_ids() == ["employee:1", "employee:2"]

    # Sync #2: "2" is deleted upstream, so only a tombstone is emitted; the
    # merge removes it while "1" (unchanged, not re-sent) stays.
    api = FakeBambooHR({"2": "Deleted"}, {}, latest="2026-07-01T00:00:00.000Z")
    pipeline.run(bamboohr_source(company_domain="acme", session=api))
    assert stored_ids() == ["employee:1"]
    # ...and the second run asked only for changes since the first run's cursor.
    assert (f"{BASE_URL}/employees/changed", {"since": "2026-06-11T16:07:32.000Z"}) in api.calls


# ---------------------------------------------------------------------------
# Company files
# ---------------------------------------------------------------------------
def _tiny_pdf(text):
    """Build a minimal one-page PDF containing ``text`` (real bytes, no mocks)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def _file(file_id, original_name, data, name=None, category="Policies"):
    return {
        "id": file_id,
        "name": name or original_name.rsplit(".", 1)[0],
        "originalFileName": original_name,
        "_data": data,
        "_category": category,
    }


def test_files_are_read_from_pdf_and_text():
    api = FakeBambooHR(
        files={
            1: _file(1, "Remote Work.pdf", _tiny_pdf("Work from home on Fridays")),
            2: _file(2, "Holidays.txt", b"Office closed on Jan 1"),
        }
    )
    state = {}

    rows = list(sync_files(api, BASE_URL, state))

    assert rows == [
        {
            "id": "file:1",
            "title": "Remote Work",
            "content": "Category: Policies\n\nWork from home on Fridays",
            "_deleted": False,
        },
        {
            "id": "file:2",
            "title": "Holidays",
            "content": "Category: Policies\n\nOffice closed on Jan 1",
            "_deleted": False,
        },
    ]
    assert state["file_ids"] == ["file:1", "file:2"]


def test_unsupported_file_types_are_not_downloaded():
    api = FakeBambooHR(files={3: _file(3, "Org chart.png", b"\x89PNG")})

    assert list(sync_files(api, BASE_URL, {})) == []
    assert f"{BASE_URL}/files/3" not in [url for url, _ in api.calls]


def test_file_missing_from_listing_becomes_tombstone():
    api = FakeBambooHR(files={2: _file(2, "Holidays.txt", b"Office closed on Jan 1")})
    state = {"file_ids": ["file:1", "file:2"]}

    rows = list(sync_files(api, BASE_URL, state))

    assert rows[-1] == {"id": "file:1", "_deleted": True}
    assert state["file_ids"] == ["file:2"]


def test_listed_but_undownloadable_file_is_forgotten():
    # Still in the listing, but the download now answers 404.
    api = FakeBambooHR(files={1: _file(1, "Old.txt", None)})

    rows = list(sync_files(api, BASE_URL, {"file_ids": ["file:1"]}))

    assert rows == [{"id": "file:1", "_deleted": True}]


def test_unparseable_file_is_skipped_but_not_forgotten():
    api = FakeBambooHR(files={1: _file(1, "Broken.pdf", b"not really a pdf")})
    state = {"file_ids": ["file:1"]}

    assert list(sync_files(api, BASE_URL, state)) == []
    # Still counted as present, so an earlier good copy is kept, not deleted.
    assert state["file_ids"] == ["file:1"]


def test_failed_listing_aborts_without_forgetting_anything():
    api = FakeBambooHR()
    api.get = lambda url, params=None, timeout=None: FakeResponse(403)
    state = {"file_ids": ["file:1"]}

    with pytest.raises(requests.HTTPError):
        list(sync_files(api, BASE_URL, state))
    assert state["file_ids"] == ["file:1"]


def test_files_can_be_switched_off():
    source = bamboohr_source(company_domain="acme", session=FakeBambooHR(), include_files=False)

    assert list(source.resources) == [EMPLOYEES_TABLE_NAME]


def test_files_resource_uses_merge_and_hard_delete():
    source = bamboohr_source(company_domain="acme", session=FakeBambooHR())
    resource = source.resources[FILES_TABLE_NAME]

    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True


def test_file_categories_limit_what_is_synced():
    api = FakeBambooHR(
        files={
            1: _file(1, "Handbook.txt", b"Be kind", category="Policies"),
            2: _file(2, "Benefits.csv", b"name,plan", category="Benefits Upgrade"),
        }
    )

    rows = list(sync_files(api, BASE_URL, {}, categories=["Policies"]))

    assert [row["id"] for row in rows] == ["file:1"]
    # Files outside the chosen categories are never downloaded.
    assert f"{BASE_URL}/files/2" not in [url for url, _ in api.calls]


def test_narrowing_categories_forgets_files_synced_before():
    api = FakeBambooHR(
        files={
            1: _file(1, "Handbook.txt", b"Be kind", category="Policies"),
            2: _file(2, "Benefits.csv", b"name,plan", category="Benefits Upgrade"),
        }
    )
    state = {"file_ids": ["file:1", "file:2"]}

    rows = list(sync_files(api, BASE_URL, state, categories=["Policies"]))

    assert rows[-1] == {"id": "file:2", "_deleted": True}
    assert state["file_ids"] == ["file:1"]
