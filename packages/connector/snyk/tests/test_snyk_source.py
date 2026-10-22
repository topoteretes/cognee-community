"""Factory validation, the HTTP client, selection and the cognee seams. No network, no LLM."""

from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_row_data import DltRowData
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.extract.exceptions import ResourceExtractionError
from fake_snyk import (
    FakeResponse,
    FakeSnykSession,
    error_response,
    make_issue,
    make_project,
)

from cognee_community_connector_snyk import snyk, snyk_source
from cognee_community_connector_snyk.snyk import (
    _Client,
    _get_json,
    _iter_rows,
    _paginate,
    _SnykConfig,
)

TOKEN = "snyk-secret-token-0123456789"
REST = "https://api.snyk.io/rest"


class RecordingLogger:
    """Stands in for the module logger so tests can read what was logged."""

    def __init__(self):
        self.lines: list[str] = []

    def _record(self, message, *args):
        self.lines.append(message % args if args else message)

    warning = info = debug = _record

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def log(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(snyk, "logger", recorder)
    monkeypatch.setattr(snyk, "_sleep", lambda seconds: None)
    for name in (
        "SNYK_TOKEN",
        "SNYK_REGION",
        "SNYK_API_URL",
        "SNYK_ORG_IDS",
        "SNYK_GROUP_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return recorder


def world(**kwargs) -> FakeSnykSession:
    """A fake Snyk with one org, two projects and two findings of one vulnerability."""
    session = FakeSnykSession(**kwargs)
    session.add_org(
        "o1",
        "Acme",
        projects=[make_project("p1", "acme/shop"), make_project("p2", "acme/admin")],
        issues=[make_issue(project="p1"), make_issue(project="p2", version="4.17.11")],
    )
    return session


def build(**overrides):
    # Each source gets its own resource name: dlt keeps the resource state of a source that is
    # iterated outside a pipeline by name, and one test's state must not reach another's.
    kwargs = {
        "api_token": TOKEN,
        "org_ids": ["o1"],
        "session": world(),
        "resource_name": f"snyk_{uuid4().hex[:8]}",
    }
    kwargs.update(overrides)
    return snyk_source(**kwargs)


def config(**overrides) -> _SnykConfig:
    values = {
        "rest_base": REST,
        "region": "us-01",
        "org_ids": ("o1",),
        "group_id": None,
        "all_orgs": False,
        "project_ids": (),
        "severities": (),
        "issue_types": ("package_vulnerability",),
        "include_resolved": False,
        "include_ignored": False,
        "include_remedies": True,
        "max_projects_per_document": 50,
        "max_issues_per_org": 20_000,
        "api_version": "2024-10-15",
        "timeout": 60.0,
    }
    values.update(overrides)
    return _SnykConfig(**values)


def client(session, **kwargs) -> _Client:
    return _Client(session, TOKEN, **kwargs)


def issue_calls(session) -> list[dict]:
    return [c for c in session.calls if urlsplit(c["url"]).path.endswith("/issues")]


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_token": None}, "API token is required"),
        ({"api_token": "abc\n"}, "whitespace"),
        ({"org_ids": None}, "Choose what to read"),
        ({"org_ids": []}, "org_ids is empty"),
        ({"org_ids": [" "]}, "org_ids is empty"),
        ({"region": "mars-01"}, "region must be one of"),
        ({"base_url": "http://api.snyk.io/rest"}, "https only"),
        ({"base_url": "https://u:p@api.snyk.io/rest"}, "must not contain credentials"),
        ({"base_url": "https://api.snyk.io/rest?x=1"}, "query or fragment"),
        ({"base_url": "https://api.snyk.io:notaport/rest"}, "not a valid URL"),
        ({"base_url": "api.snyk.io"}, "https only"),
        ({"severities": ["urgent"]}, "unknown values"),
        ({"severities": []}, "severities is empty"),
        ({"issue_types": ("nonsense",)}, "unknown values"),
        ({"issue_types": ()}, "issue_types is empty"),
        ({"issue_types": ("code",)}, "not supported"),
        ({"issue_types": ("package_vulnerability", "secrets")}, "not supported"),
        ({"project_ids": []}, "project_ids is empty"),
        ({"max_projects_per_document": 0}, "at least 1"),
        ({"max_issues_per_org": 0}, "at least 1"),
        ({"timeout": 0}, "timeout"),
        ({"allow_mass_delete": "yes"}, "allow_mass_delete"),
        ({"api_version": "latest"}, "api_version"),
        ({"api_version": "2024-13-45"}, "api_version"),
        ({"resource_name": " "}, "resource_name"),
    ],
)
def test_invalid_configuration_is_refused_without_echoing_secrets(overrides, message):
    with pytest.raises(ValueError, match=message) as error:
        build(**overrides)
    assert TOKEN not in str(error.value)


def test_a_group_or_all_orgs_is_enough_to_choose_what_to_read():
    build(org_ids=None, group_id="g1")
    build(org_ids=None, all_orgs=True)


def test_the_regions_pick_their_host_and_the_region_is_part_of_the_ids():
    for region, host in [
        ("us-01", "https://api.snyk.io"),
        ("us-02", "https://api.us.snyk.io"),
        ("EU-01", "https://api.eu.snyk.io"),
        ("au-01", "https://api.au.snyk.io"),
    ]:
        session = FakeSnykSession(base=f"{host}/rest")
        session.add_org("o1", "Acme", issues=[make_issue()], projects=[make_project("p1", "x")])
        rows = list(build(region=region, session=session))
        assert session.calls[0]["url"].startswith(f"{host}/rest/orgs/o1")
        assert rows[0]["id"].startswith(f"snyk:{region.lower()}:o1:vuln:")


def test_base_url_overrides_the_region_host():
    session = FakeSnykSession(base="https://snyk.internal.example/rest")
    session.add_org("o1", issues=[make_issue()], projects=[make_project("p1", "x")])
    rows = list(build(base_url="https://snyk.internal.example/", region="eu-01", session=session))
    assert session.calls[0]["url"] == "https://snyk.internal.example/rest/orgs/o1"
    assert rows[0]["id"].startswith("snyk:eu-01:")


def test_environment_variables_are_the_fallback(monkeypatch):
    monkeypatch.setenv("SNYK_TOKEN", TOKEN)
    monkeypatch.setenv("SNYK_REGION", "eu-01")
    monkeypatch.setenv("SNYK_ORG_IDS", "o1, o2")
    session = FakeSnykSession(base="https://api.eu.snyk.io/rest")
    session.add_org("o1")
    session.add_org("o2")
    list(snyk_source(session=session, resource_name=f"snyk_{uuid4().hex[:8]}"))
    urls = [c["url"] for c in session.calls if c["url"].endswith(("/o1", "/o2"))]
    assert urls == [
        "https://api.eu.snyk.io/rest/orgs/o1",
        "https://api.eu.snyk.io/rest/orgs/o2",
    ]


def test_group_and_api_url_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SNYK_TOKEN", TOKEN)
    monkeypatch.setenv("SNYK_GROUP_ID", "g1")
    monkeypatch.setenv("SNYK_API_URL", "https://api.au.snyk.io/rest")
    session = FakeSnykSession(base="https://api.au.snyk.io/rest")
    session.add_org("o1")
    session.group_orgs["g1"] = ["o1"]
    list(snyk_source(session=session, resource_name=f"snyk_{uuid4().hex[:8]}"))
    assert session.calls[0]["url"] == "https://api.au.snyk.io/rest/groups/g1/orgs"


def test_arguments_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("SNYK_ORG_IDS", "from-env")
    session = world()
    list(build(org_ids=["o1"], session=session))
    assert session.calls[0]["url"] == f"{REST}/orgs/o1"


def test_the_token_never_appears_in_logs_stats_or_state(log):
    resource = build()
    list(resource)
    assert TOKEN not in log.text
    assert TOKEN not in str(resource.cognee_sync_stats)
    assert TOKEN not in repr(client(world()))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def get(session, **kwargs):
    return _get_json(client(session, **kwargs.pop("client", {})), f"{REST}/orgs/o1", **kwargs)


def test_headers_timeout_and_version_are_sent():
    session = world()
    list(build(session=session, timeout=12.5))
    call = session.calls[0]
    assert call["headers"] == {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.api+json",
    }
    assert call["timeout"] == 12.5
    assert ("version", "2024-10-15") in [c for call in session.calls for c in call["params"]]


def test_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(snyk, "_sleep", sleeps.append)
    session = world()
    session.script = [FakeResponse(503, {}), FakeResponse(429, {}), FakeResponse(502, {})]
    assert get(session)["data"]["id"] == "o1"
    assert len(session.calls) == 4
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[2]  # exponential, with up to a second of jitter


def test_retry_after_is_honoured(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(snyk, "_sleep", sleeps.append)
    session = world()
    session.script = [FakeResponse(429, {}, {"Retry-After": "7"})]
    get(session)
    assert sleeps == [7.0]


def test_a_wait_longer_than_the_cap_raises_without_sleeping(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(snyk, "_sleep", sleeps.append)
    session = world()
    session.script = [FakeResponse(429, {}, {"Retry-After": "61"})]
    with pytest.raises(RuntimeError, match="61s"):
        get(session)
    assert sleeps == []


def test_the_run_wide_wait_budget_raises(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(snyk, "_sleep", sleeps.append)
    session = world()
    session.script = [FakeResponse(429, {}, {"Retry-After": "50"})] * 3
    with pytest.raises(RuntimeError, match="120s in all"):
        get(session)
    assert sleeps == [50.0, 50.0]


def test_retry_exhaustion_raises_after_five_attempts():
    session = world()
    session.script = [error_response(503, "busy")] * 5
    with pytest.raises(RuntimeError, match=r"HTTP 503.*5 attempts"):
        get(session)
    assert len(session.calls) == 5


def test_401_mentions_the_region():
    session = world()
    session.script = [error_response(401, "Invalid auth token provided")]
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        get(session)
    assert "region" in str(error.value)
    assert "Invalid auth token provided" in str(error.value)
    assert len(session.calls) == 1


def test_403_explains_the_enterprise_requirement_and_permissions():
    session = world()
    session.script = [error_response(403, "Forbidden")]
    with pytest.raises(RuntimeError, match="HTTP 403") as error:
        get(session)
    message = str(error.value)
    assert "Enterprise" in message
    assert "org.project.snapshot.read" in message
    assert "group.issues.read" in message


def test_404_names_what_was_not_found():
    session = world()
    session.script = [error_response(404, "Org not found")]
    with pytest.raises(RuntimeError, match="organization o9"):
        get(session, what="organization o9")


def test_the_server_detail_is_clipped_and_the_token_is_cut_out():
    session = world()
    session.script = [error_response(400, f"bad token {TOKEN} " + "x" * 500)]
    with pytest.raises(RuntimeError, match="HTTP 400") as error:
        get(session)
    assert TOKEN not in str(error.value)
    assert "[token]" in str(error.value)
    assert len(str(error.value)) < 500


def test_a_body_that_is_not_json_is_explained():
    session = world()
    session.script = [FakeResponse(200, "<html>login</html>")]
    with pytest.raises(RuntimeError, match="not JSON"):
        get(session)


@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectionError(TOKEN), requests.exceptions.ReadTimeout("slow")]
)
def test_connection_problems_come_with_a_region_hint_and_no_details(error):
    session = world()
    session.script = [error]
    with pytest.raises(RuntimeError, match=r"api\.eu\.snyk\.io") as raised:
        get(session)
    assert TOKEN not in str(raised.value)


def test_check_active_runs_before_every_request_and_every_wait(monkeypatch):
    calls: list[str] = []
    session = world()
    session.script = [FakeResponse(503, {})]
    monkeypatch.setattr(snyk, "_sleep", lambda s: calls.append("sleep"))
    get(session, client={"check_active": lambda: calls.append("check")})
    assert calls == ["check", "check", "sleep", "check"]


def test_check_active_can_stop_the_run():
    def stop():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        get(world(), client={"check_active": stop})


# --- pagination -------------------------------------------------------------
def many_issues(count):
    return [make_issue(project="p1", snyk_id=f"SNYK-JS-PKG-{i}", cves=()) for i in range(count)]


def listing(session, **overrides):
    return list(_paginate(client(session), config(**overrides), "/orgs/o1/issues"))


@pytest.mark.parametrize("style", ["absolute", "relative", "object"])
def test_next_links_are_followed_in_every_style(style):
    session = FakeSnykSession(page_size=10, link_style=style)
    session.add_org("o1", issues=many_issues(25))
    assert len(listing(session)) == 25
    assert len(issue_calls(session)) == 3
    first = issue_calls(session)[0]
    assert ("limit", "100") in first["params"]
    assert ("version", "2024-10-15") in first["params"]


def test_the_version_is_added_when_a_link_lacks_it():
    session = FakeSnykSession(page_size=10)
    session.version_in_links = False
    session.add_org("o1", issues=many_issues(15))
    assert len(listing(session)) == 15
    assert "version=2024-10-15" in issue_calls(session)[1]["url"]


def test_a_link_to_another_host_is_refused():
    session = FakeSnykSession()
    session.script = [
        FakeResponse(200, {"data": [], "links": {"next": "https://evil.example.com/rest/x"}})
    ]
    with pytest.raises(RuntimeError, match="another host"):
        listing(session)


def test_a_looping_link_is_detected():
    session = FakeSnykSession()
    loop = f"{REST}/orgs/o1/issues?starting_after=1"
    session.script = [FakeResponse(200, {"data": [], "links": {"next": loop}})] * 3
    with pytest.raises(RuntimeError, match="loops"):
        listing(session)


def test_the_page_cap_stops_an_endless_listing(monkeypatch):
    monkeypatch.setattr(snyk, "MAX_PAGES", 3)
    session = FakeSnykSession()
    session.script = [
        FakeResponse(
            200, {"data": [], "links": {"next": f"{REST}/orgs/o1/issues?starting_after={i}"}}
        )
        for i in range(10)
    ]
    with pytest.raises(RuntimeError, match="more than 3 pages"):
        listing(session)


def test_a_missing_data_or_links_key_means_an_empty_page():
    session = FakeSnykSession()
    session.script = [FakeResponse(200, {})]
    assert listing(session) == []


# ---------------------------------------------------------------------------
# Selection and request parameters
# ---------------------------------------------------------------------------
def two_orgs():
    session = FakeSnykSession()
    for org_id in ("o2", "o1"):
        session.add_org(
            org_id,
            f"Org {org_id}",
            projects=[make_project(f"{org_id}-p", f"{org_id}/repo")],
            issues=[make_issue(project=f"{org_id}-p", org=org_id)],
        )
    return session


def test_explicit_orgs_are_read_in_id_order_with_their_names():
    session = two_orgs()
    rows = list(build(org_ids=["o2", "o1", "o2"], session=session))
    assert [r["id"].split(":")[2] for r in rows] == ["o1", "o2"]
    assert "Organization: Org o1 (us-01)" in rows[0]["content"]


def test_a_group_lists_its_orgs():
    session = two_orgs()
    session.group_orgs["g1"] = ["o1", "o2"]
    rows = list(build(org_ids=None, group_id="g1", session=session))
    assert len(rows) == 2
    assert session.calls[0]["url"].endswith("/groups/g1/orgs")


def test_all_orgs_lists_every_readable_org():
    session = two_orgs()
    rows = list(build(org_ids=None, all_orgs=True, session=session))
    assert len(rows) == 2
    assert session.calls[0]["url"].endswith("/orgs")


def test_sources_are_combined_and_deduplicated():
    session = two_orgs()
    session.group_orgs["g1"] = ["o1"]
    rows = list(build(org_ids=["o1"], group_id="g1", all_orgs=True, session=session))
    assert len(rows) == 2


def test_an_explicit_org_that_cannot_be_read_raises():
    session = two_orgs()
    # dlt wraps whatever a resource raises while it is being extracted.
    with pytest.raises(ResourceExtractionError, match="organization nope"):
        list(build(org_ids=["o1", "nope"], session=session))
    session.script = [error_response(403, "no")]
    with pytest.raises(ResourceExtractionError, match="HTTP 403"):
        list(build(org_ids=["o1"], session=session))


def test_project_ids_limit_the_issue_requests_to_those_projects():
    session = world()
    rows = list(build(project_ids=["p2"], session=session))
    requests_ = issue_calls(session)
    assert len(requests_) == 1
    assert ("scan_item.id", "p2") in requests_[0]["params"]
    assert ("scan_item.type", "project") in requests_[0]["params"]
    assert "acme/admin" in rows[0]["content"]
    assert "acme/shop" not in rows[0]["content"]


def test_one_issue_request_per_project_and_type():
    session = world()
    list(
        build(
            project_ids=["p1", "p2"],
            issue_types=("package_vulnerability", "license"),
            session=session,
        )
    )
    assert len(issue_calls(session)) == 4


def test_an_unknown_project_id_raises():
    with pytest.raises(ResourceExtractionError, match=r"not found.*nope"):
        list(build(project_ids=["p1", "nope"]))


def test_an_org_without_the_requested_projects_is_skipped():
    session = two_orgs()
    rows = list(build(org_ids=["o1", "o2"], project_ids=["o2-p"], session=session))
    assert [r["id"].split(":")[2] for r in rows] == ["o2"]
    assert all("/o1/issues" not in c["url"] for c in session.calls)


def params_of(session) -> dict:
    return dict(issue_calls(session)[0]["params"])


def test_the_default_filters_are_open_not_ignored_package_vulnerabilities():
    session = world()
    list(build(session=session))
    assert params_of(session) == {
        "version": "2024-10-15",
        "limit": "100",
        "type": "package_vulnerability",
        "status": "open",
        "ignored": "false",
    }


def test_include_resolved_and_include_ignored_change_the_filters():
    session = world()
    list(build(session=session, include_resolved=True))
    assert params_of(session)["status"] == "open,resolved"
    assert params_of(session)["ignored"] == "false"
    session = world()
    list(build(session=session, include_ignored=True))
    assert "ignored" not in params_of(session)


def test_severities_are_sent_as_one_comma_list():
    session = world()
    list(build(session=session, severities=["high", "critical", "high"]))
    assert params_of(session)["effective_severity_level"] == "high,critical"


def test_no_code_flows_are_ever_requested():
    session = world()
    list(build(session=session))
    names = {key for call in session.calls for key, _ in call["params"]}
    assert "include_code_flows" not in names


# ---------------------------------------------------------------------------
# cognee seams
# ---------------------------------------------------------------------------
def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = build(resource_name="acme_snyk")
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "snyk"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "acme_snyk"
    assert resource.cognee_sync_stats == {}
    assert resource.name == "acme_snyk"


def test_the_default_resource_name():
    source = snyk_source(api_token=TOKEN, org_ids=["o1"], session=world())
    assert source.name == "snyk_vulnerabilities"


def test_resource_declares_merge_with_a_hard_delete_column():
    schema = build().compute_table_schema()
    disposition = schema["write_disposition"]
    assert (
        disposition.get("disposition") if isinstance(disposition, dict) else disposition
    ) == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_old_cognee_without_document_sync_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match="table-scoped"):
        build()


def test_rows_pass_through_guarded_rows():
    calls: list[str] = []
    rows = list(build(check_active=lambda: calls.append("check")))
    assert len(rows) == 1
    assert len(calls) >= 6  # requests, then before and after the row, and at the end


def test_a_failing_check_active_surfaces_through_dlt():
    def stop():
        raise PermissionError("revoked")

    with pytest.raises(ResourceExtractionError, match="revoked"):
        list(build(check_active=stop))


def test_rows_become_cognee_document_items():
    session = world()
    row = next(_iter_rows(client(session), config(), {}))
    assert set(row) == {"id", "title", "content", "_deleted"}  # url only when Snyk gives one
    assert row["_deleted"] is False
    loaded = DltRowData(
        table_name="snyk_vulnerabilities",
        primary_key_column="id",
        primary_key_value=row["id"],
        row_data=row,
        content_hash="h",
        schema_info=[],
        schema_hash="s",
        foreign_keys=[],
        dlt_db_name="db",
        dataset_name="ds",
    )
    item = _build_document_data_item(loaded, uuid4(), "snyk")
    assert item.system_metadata["source"] == "snyk"
    assert item.system_metadata["external_id"] == row["id"]
    assert item.data.startswith("# Snyk vulnerability SNYK-JS-LODASH-567746")


def test_a_session_that_is_not_injected_is_created_and_closed(monkeypatch):
    created = []

    class Recorded(FakeSnykSession):
        def close(self):
            created.append("closed")

    session = Recorded()
    session.add_org("o1")
    monkeypatch.setattr(snyk, "_make_session", lambda: session)
    list(snyk_source(api_token=TOKEN, org_ids=["o1"], resource_name=f"snyk_{uuid4().hex[:8]}"))
    assert created == ["closed"]
