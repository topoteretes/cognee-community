"""Dependabot alert ingestion tests using a fake GitHub API."""

from __future__ import annotations

import copy
import json

import dlt
import httpx
import pytest

from cognee_community_connector_github.github import (
    API_ROOT,
    _dependabot_alert_to_row,
    _repo_rows,
    github_source,
)


def _alert(number: int = 7, state: str = "open") -> dict:
    return {
        "number": number,
        "state": state,
        "dependency": {
            "package": {"ecosystem": "pip", "name": "django"},
            "manifest_path": "requirements.txt",
            "scope": "runtime",
        },
        "security_advisory": {
            "ghsa_id": "GHSA-rf4j-j272-fj86",
            "cve_id": "CVE-2018-6188",
            "summary": "Example advisory",
            "description": "Example description",
            "severity": "high",
            "references": [{"url": "https://b.example"}, {"url": "https://a.example"}],
        },
        "security_vulnerability": {
            "severity": "high",
            "vulnerable_version_range": ">= 2.0.0, < 2.0.2",
            "first_patched_version": {"identifier": "2.0.2"},
        },
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "fixed_at": None,
        "dismissed_at": None,
        "dismissed_reason": None,
        "dismissed_comment": "Only included when requested",
    }


def _response(request, payload, *, status_code: int = 200, headers: dict | None = None):
    response_headers = {"content-type": "application/json", **(headers or {})}
    return httpx.Response(
        status_code,
        json=payload,
        headers=response_headers,
        request=request,
    )


def _repo_handler(request: httpx.Request):
    path = request.url.path
    if path == "/repos/o/r":
        return _response(request, {"id": 1234, "full_name": "o/r", "description": "repo"})
    if path.endswith(("/issues", "/pulls", "/commits", "/releases")):
        return _response(request, [])
    return _response(request, [], status_code=404)


def test_alert_row_has_stable_identity_and_deterministic_content():
    alert = _alert()
    row = _dependabot_alert_to_row(alert, 1234, "o/r")
    repeated = _dependabot_alert_to_row(copy.deepcopy(alert), 1234, "o/r")

    assert row["id"] == "dependabot_alert:1234#7"
    assert row["url"] == "https://github.com/o/r/security/dependabot/7"
    assert row["content"] == repeated["content"]
    assert '"manifest_path": "requirements.txt"' in row["content"]
    assert '"first_patched_version": "2.0.2"' in row["content"]
    assert (
        '"references": [\n      "https://a.example",\n      "https://b.example"' in row["content"]
    )
    assert '"comment": null' in row["content"]
    assert "Only included when requested" not in row["content"]


def test_alert_resolution_updates_content_without_changing_identity():
    open_row = _dependabot_alert_to_row(_alert(state="open"), 1234, "o/r")
    fixed_alert = _alert(state="fixed")
    fixed_alert["fixed_at"] = "2026-02-01T00:00:00Z"
    fixed_row = _dependabot_alert_to_row(fixed_alert, 1234, "o/r")

    assert open_row["id"] == fixed_row["id"]
    assert open_row["content"] != fixed_row["content"]
    assert '"state": "fixed"' in fixed_row["content"]
    assert '"fixed_at": "2026-02-01T00:00:00Z"' in fixed_row["content"]


def test_alert_row_handles_null_optional_fields():
    row = _dependabot_alert_to_row(
        {"number": 11, "dependency": None, "security_advisory": None},
        1234,
        "o/r",
    )

    details = json.loads(row["content"])
    assert details["dependency"]["manifest_path"] is None
    assert details["advisory"]["ghsa_id"] is None
    assert details["vulnerability"]["first_patched_version"] is None
    assert details["dates"]["fixed_at"] is None


def test_dismissal_comment_is_included_only_when_opted_in():
    alert = _alert(state="dismissed")
    alert["dismissed_reason"] = "tolerable_risk"
    default_row = _dependabot_alert_to_row(alert, 1234, "o/r")
    opted_in_row = _dependabot_alert_to_row(alert, 1234, "o/r", include_dismissal_comments=True)

    assert "Only included when requested" not in default_row["content"]
    assert "Only included when requested" in opted_in_row["content"]
    assert '"reason": "tolerable_risk"' in default_row["content"]


def test_dependabot_requires_explicit_repositories():
    with pytest.raises(ValueError, match="explicitly list repositories"):
        github_source(token="fake", include_dependabot_alerts=True)


def test_dependabot_fetches_all_states_and_follows_pagination():
    seen_requests: list[httpx.Request] = []
    states = ["open", "fixed", "dismissed", "auto_dismissed"]

    def handler(request: httpx.Request):
        seen_requests.append(request)
        if request.url.path.endswith("/dependabot/alerts"):
            if request.url.params.get("page") == "2":
                return _response(request, [_alert(9, "dismissed"), _alert(10, "auto_dismissed")])
            next_url = (
                f"{API_ROOT}/repos/o/r/dependabot/alerts?state=open%2Cfixed%2Cdismissed%2C"
                "auto_dismissed&per_page=100&page=2"
            )
            return _response(
                request,
                [_alert(7, "open"), _alert(8, "fixed")],
                headers={"link": f'<{next_url}>; rel="next"'},
            )
        return _repo_handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = _repo_rows(client, "o/r", include_dependabot_alerts=True)

    alert_rows = [row for row in rows if row["id"].startswith("dependabot_alert:")]
    assert [row["id"] for row in alert_rows] == [
        "dependabot_alert:1234#7",
        "dependabot_alert:1234#8",
        "dependabot_alert:1234#9",
        "dependabot_alert:1234#10",
    ]
    assert {json.loads(row["content"])["state"] for row in alert_rows} == set(states)
    alert_requests = [request for request in seen_requests if request.url.path.endswith("/alerts")]
    assert len(alert_requests) == 2
    assert alert_requests[0].url.params.get("state") == ",".join(states)
    assert alert_requests[1].url.params.get("page") == "2"


def test_alert_identity_is_isolated_by_repository():
    first = _dependabot_alert_to_row(_alert(), 1234, "o/r")
    second = _dependabot_alert_to_row(_alert(), 5678, "o/other")

    assert first["id"] != second["id"]


def test_failed_alert_page_preserves_previous_dlt_snapshot(tmp_path):
    fail_second_page = False

    def handler(request: httpx.Request):
        if request.url.path.endswith("/dependabot/alerts"):
            if request.url.params.get("page") == "2":
                if fail_second_page:
                    return _response(request, {"message": "Forbidden"}, status_code=403)
                return _response(request, [_alert(8, "fixed")])
            if fail_second_page:
                next_url = (
                    f"{API_ROOT}/repos/o/r/dependabot/alerts?state=open%2Cfixed%2Cdismissed%2C"
                    "auto_dismissed&per_page=100&page=2"
                )
                return _response(
                    request,
                    [_alert(7)],
                    headers={"link": f'<{next_url}>; rel="next"'},
                )
            return _response(request, [_alert(7)])
        return _repo_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    db_path = (tmp_path / "dependabot.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="dependabot_snapshot_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="github",
        pipelines_dir=str(tmp_path / "state"),
    )

    def source():
        return github_source(
            token="fake",
            repos=["o/r"],
            include_dependabot_alerts=True,
            client=client,
        )

    try:
        pipeline.run(source())
        with (
            pipeline.sql_client() as sql_client,
            sql_client.execute_query(
                "SELECT id, content FROM github_documents ORDER BY id"
            ) as cursor,
        ):
            previous_rows = cursor.fetchall()

        assert any(row[0] == "dependabot_alert:1234#7" for row in previous_rows)

        fail_second_page = True
        with pytest.raises(Exception, match="aborting the snapshot"):
            pipeline.run(source())

        with (
            pipeline.sql_client() as sql_client,
            sql_client.execute_query(
                "SELECT id, content FROM github_documents ORDER BY id"
            ) as cursor,
        ):
            rows_after_failure = cursor.fetchall()

        assert rows_after_failure == previous_rows
    finally:
        client.close()
