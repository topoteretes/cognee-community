"""Unit tests for the Snyk dlt connector.

Two layers, all runnable in CI without a live Snyk token:

* DB-free tests for CVE extraction/dedupe, issue→row flattening, cursor
  pagination, retry classification, and the generic document DataItem tagging
  (``source="snyk"``) that routes issues through normal cognify.
* dlt-pipeline tests (mocked httpx transport, temp sqlite destination)
  covering the acceptance criteria: re-sync reflects edits, and fixed/vanished
  issues drop out of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_snyk.snyk import (
    SNYK_SOURCE_NAME,
    _issue_cves,
    _issue_to_row,
    _paginate,
    _retry_after,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _issue(issue_id, title="Denial of Service (DoS)", cves=None, **attrs):
    attributes = {
        "title": title,
        "severity": "high",
        "packageName": "django",
        "introducedDate": "2024-01-10T00:00:00.000Z",
        "description": "A DoS vector in the parser.",
        "remediation": "Upgrade to 4.2.7.",
        **attrs,
    }
    if cves is not None:
        attributes["problems"] = [{"id": cve, "source": "CVE"} for cve in cves]
    return {"id": issue_id, "type": "issue", "attributes": attributes}


def _transport(handler):
    import httpx

    return httpx.MockTransport(handler)


def _json_response(payload, status=200):
    import httpx

    return httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# CVE extraction / dedupe (DB-free)
# ---------------------------------------------------------------------------


def test_issue_cves_reads_problems():
    issue = _issue("i1", cves=["CVE-2024-1234", "CVE-2024-5678"])
    assert _issue_cves(issue) == ["CVE-2024-1234", "CVE-2024-5678"]


def test_issue_cves_reads_identifiers_shape():
    issue = _issue("i1", cves=None)
    issue["attributes"]["identifiers"] = {"CVE": ["CVE-2023-9999"], "GHSA": ["GHSA-x"]}
    assert _issue_cves(issue) == ["CVE-2023-9999"]


def test_issue_cves_empty_without_cves():
    assert _issue_cves(_issue("i1", cves=None)) == []
    assert _issue_cves({}) == []


# ---------------------------------------------------------------------------
# Issue → row (DB-free)
# ---------------------------------------------------------------------------


def test_issue_to_row_flattens_issue():
    row = _issue_to_row(_issue("i1", cves=["CVE-2024-1234"]))

    # Only identity/provenance + text are kept — no volatile bookkeeping, so a
    # metadata-only change does not churn the content-hash data_id.
    assert row["id"] == "i1"
    assert row["title"] == "Denial of Service (DoS)"
    assert "high" in row["content"]
    assert "CVE-2024-1234" in row["content"]
    assert "django" in row["content"]
    assert "Upgrade to 4.2.7." in row["content"]
    assert "introducedDate" not in row
    assert set(row) == {"id", "url", "title", "content", "_dedupe_key"}


def test_issue_to_row_dedupe_key_prefers_cve():
    assert _issue_to_row(_issue("i1", cves=["CVE-2024-1234"]))["_dedupe_key"] == "CVE-2024-1234"
    assert _issue_to_row(_issue("i1", cves=None))["_dedupe_key"] == "snyk:i1"


def test_issue_to_row_url_prefers_snyk_vuln_page():
    issue = _issue("i1", cves=None, key="SNYK-PYTHON-DJANGO-7642790")
    assert _issue_to_row(issue)["url"] == "https://security.snyk.io/vuln/SNYK-PYTHON-DJANGO-7642790"


# ---------------------------------------------------------------------------
# Pagination / retry (DB-free)
# ---------------------------------------------------------------------------


def test_paginate_follows_next_link():
    import httpx

    first = {"data": [_issue("i1")], "links": {"next": "/orgs/o/issues?cursor=abc"}}
    second = {"data": [_issue("i2")], "links": {}}
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "cursor=abc" in str(request.url):
            return _json_response(second)
        return _json_response(first)

    client = httpx.Client(transport=_transport(handler), base_url="https://api.snyk.io/rest")
    assert [i["id"] for i in _paginate(client, "o")] == ["i1", "i2"]
    assert len(seen) == 2


def test_paginate_stops_on_repeated_next():
    import httpx

    loop = {"data": [_issue("i1")], "links": {"next": "/orgs/o/issues?cursor=same"}}

    def handler(request):
        return _json_response(loop)

    client = httpx.Client(transport=_transport(handler), base_url="https://api.snyk.io/rest")
    assert [i["id"] for i in _paginate(client, "o")] == ["i1", "i1"]


def test_retry_after_prefers_header():
    assert _retry_after({"retry-after": "3"}, 0) == 3.0
    assert _retry_after({"Retry-After": "2"}, 0) == 2.0
    assert _retry_after({}, 2) == 4.0


def test_request_retries_429_then_succeeds(monkeypatch):
    import httpx

    from cognee_community_connector_snyk.snyk import _request

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"errors": []})
        return _json_response({"data": []})

    monkeypatch.setattr("time.sleep", lambda _: None)
    client = httpx.Client(transport=_transport(handler), base_url="https://x.test")
    assert _request(client, "/orgs/o/issues", None) == {"data": []}
    assert len(calls) == 2


def test_request_propagates_auth_errors():
    import httpx

    from cognee_community_connector_snyk.snyk import _request

    def handler(request):
        return httpx.Response(401, json={"errors": []})

    client = httpx.Client(transport=_transport(handler), base_url="https://x.test")
    with pytest.raises(httpx.HTTPStatusError):
        _request(client, "/orgs/o/issues", None)


def test_snyk_source_requires_credentials(monkeypatch):
    from cognee_community_connector_snyk.snyk import snyk_source

    monkeypatch.delenv("SNYK_TOKEN", raising=False)
    monkeypatch.delenv("SNYK_ORG_ID", raising=False)
    with pytest.raises(ValueError, match="token"):
        snyk_source(token=None, org_id="o")
    with pytest.raises(ValueError, match="organization"):
        snyk_source(token="t", org_id=None)


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "i1",
            "url": "https://security.snyk.io/vuln/SNYK-PYTHON-DJANGO-7642790",
            "title": "Denial of Service (DoS)",
            "content": "high django body",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "i1")

    item = _build_document_data_item(row, data_id, "snyk")

    # source="snyk" (not "dlt") is what routes the issue through normal cognify.
    assert item.external_metadata["source"] == "snyk"
    assert (
        item.external_metadata["url"] == "https://security.snyk.io/vuln/SNYK-PYTHON-DJANGO-7642790"
    )
    assert item.external_metadata["external_id"] == "i1"
    assert item.data_id == data_id
    assert item.data.startswith("# Denial of Service (DoS)")
    assert "high django body" in item.data


def test_snyk_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_snyk.snyk import snyk_source

    source = snyk_source(token="test-token", org_id="test-org")
    assert SNYK_SOURCE_NAME == "snyk"
    assert document_source_tag(source) == "snyk"


def test_library_has_no_print_calls():
    from pathlib import Path

    lib = Path(__file__).parent.parent / "cognee_community_connector_snyk" / "snyk.py"
    assert "print(" not in lib.read_text()


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt + httpx)
# ---------------------------------------------------------------------------


def _stateful_handler(state):
    def handler(request):
        return _json_response({"data": state["issues"], "links": {}})

    return handler


def _run_sync(dlt_mod, tmp_path, issues):
    """Run snyk_source through a dlt pipeline into a temp sqlite destination."""
    import httpx

    from cognee_community_connector_snyk.snyk import snyk_source

    db_path = (tmp_path / "snyk.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="snyk_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="snyk_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    state = {"issues": issues}
    client = httpx.Client(
        transport=_transport(_stateful_handler(state)), base_url="https://api.snyk.io/rest"
    )
    pipeline.run(snyk_source(token="t", org_id="o", client=client))
    return pipeline


def _read_issues(pipeline):
    """Return {id: row-dict} for the snyk_issues table.

    Reads positionally (the SELECT fixes the column order) since dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM snyk_issues") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_issues_with_content(dlt_mod, tmp_path):
    pipeline = _run_sync(dlt_mod, tmp_path, [_issue("i1", cves=["CVE-2024-1234"])])

    rows = _read_issues(pipeline)
    assert set(rows) == {"i1"}
    assert "CVE-2024-1234" in rows["i1"]["content"]


def test_shared_cve_is_deduplicated(dlt_mod, tmp_path):
    issues = [
        _issue("i1", cves=["CVE-2024-1234"]),
        _issue("i2", cves=["CVE-2024-1234"], packageName="flask"),
    ]
    pipeline = _run_sync(dlt_mod, tmp_path, issues)

    rows = _read_issues(pipeline)
    # Same CVE across projects: first occurrence wins, per the issue's dedupe rule.
    assert set(rows) == {"i1"}


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [_issue("i1", description="v1")])

    pipeline = _run_sync(dlt_mod, tmp_path, [_issue("i1", description="v2")])

    rows = _read_issues(pipeline)
    assert "v2" in rows["i1"]["content"]
    assert "v1" not in rows["i1"]["content"]


def test_fixed_issue_is_removed_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [_issue("i1"), _issue("i2")])

    # i1 remediated: Snyk drops it from the listing, so it is absent from the
    # replace load and falls out of staging.
    pipeline = _run_sync(dlt_mod, tmp_path, [_issue("i2")])

    rows = _read_issues(pipeline)
    # Absent from staging → orphan cleanup forgets it downstream.
    assert "i1" not in rows
    assert "i2" in rows


def test_request_error_aborts_sync(dlt_mod, tmp_path):
    # A mid-sync failure must abort the run (leaving staging/memory intact),
    # not commit a partial snapshot that orphan cleanup reconciles as fixed.
    import httpx

    from cognee_community_connector_snyk.snyk import snyk_source

    def boom(request):
        raise httpx.ConnectError("network down")

    client = httpx.Client(transport=_transport(boom), base_url="https://api.snyk.io/rest")
    db_path = (tmp_path / "boom.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="snyk_boom",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="snyk_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(snyk_source(token="t", org_id="o", client=client))
