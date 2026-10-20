import json
from datetime import UTC, datetime

import pytest

from cognee_community_connector_logrocket.logrocket import (
    IncompleteLogRocketResultError,
    _fetch_rows,
    issue_to_row,
    logrocket_source,
    remember_logrocket,
    session_to_row,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def search(self, name, arguments):
        self.calls.append((name, arguments))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def session(**updates):
    value = {
        "recordingID": "rec-1",
        "sessionID": 7,
        "sessionURL": "https://app.logrocket.com/org/project/s/rec-1/7",
        "date": 1_760_000_000_000,
        "durationMs": 1250,
        "browserInfo": {
            "browser": "Chrome",
            "browserVersion": "140",
            "os": "macOS",
            "osVersion": "15",
            "deviceName": None,
        },
        "location": {
            "city": "Pune",
            "region": "MH",
            "country": "India",
            "ip": "203.0.113.10",
        },
        "user": {
            "userID": "test-user",
            "name": "Secret Name",
            "email": "secret@example.test",
        },
    }
    value.update(updates)
    return value


def issue(**updates):
    value = {
        "url": "https://app.logrocket.com/org/project/issue/fixture",
        "issueType": "exception",
        "earliest": "2026-10-19T10:00:00Z",
        "latest": "2026-10-19T11:00:00Z",
        "eventCount": 2,
        "platformType": "web",
        "triageStatus": "untriaged",
        "group": {"filters": []},
        "messages": [
            {
                "title": "Failure for secret@example.test from 203.0.113.10",
                "message": "Checkout failed after submitting payment",
                "href": "https://private.example.test/path?token=secret",
            }
        ],
    }
    value.update(updates)
    return value


def fetch(client, resource, **kwargs):
    return list(
        _fetch_rows(
            client,
            resource,
            organization_id="org",
            project_id="project",
            limit=kwargs.pop("limit", 5),
            lookback_days=kwargs.pop("lookback_days", 30 if resource == "issues" else 7),
            window_start=kwargs.pop("window_start", None),
            window_end=kwargs.pop("window_end", None),
            query=kwargs.pop("query", None),
            include_user_id=kwargs.pop("include_user_id", False),
            include_location=kwargs.pop("include_location", False),
            now=kwargs.pop("now", datetime(2026, 10, 19, 12, tzinfo=UTC)),
            **kwargs,
        )
    )


def test_session_row_has_stable_identity_and_private_defaults():
    row = session_to_row(session(), "org", "project")
    assert row["id"] == "logrocket:org:project:session:rec-1:7"
    assert row["url"].endswith("/rec-1/7")
    serialized = json.dumps(row)
    assert "secret@example.test" not in serialized
    assert "Secret Name" not in serialized
    assert "203.0.113.10" not in serialized
    assert "test-user" not in serialized


def test_session_privacy_options_include_only_selected_fields():
    row = session_to_row(session(), "org", "project", include_user_id=True, include_location=True)
    assert "User ID: test-user" in row["content"]
    assert "City: Pune" in row["content"]
    assert "203.0.113.10" not in row["content"]
    assert "secret@example.test" not in row["content"]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"recordingID": None}, "recordingID"),
        ({"sessionID": "7"}, "sessionID"),
        ({"date": "yesterday"}, "date"),
        ({"browserInfo": []}, "browserInfo"),
    ],
)
def test_session_parser_fails_closed_on_shape_changes(updates, message):
    with pytest.raises(ValueError, match=message):
        session_to_row(session(**updates), "org", "project")


def test_issue_row_uses_repeatable_url_hash_and_redacts_free_text():
    first = issue_to_row(issue(), "org", "project")
    second = issue_to_row(issue(), "org", "project")
    assert first["id"] == second["id"]
    assert "issue-url-sha256" in first["id"]
    serialized = json.dumps(first)
    assert "secret@example.test" not in serialized
    assert "203.0.113.10" not in serialized
    assert "private.example.test" not in serialized
    assert "[redacted-email]" in first["title"]


def test_issue_parser_requires_observed_shape():
    with pytest.raises(ValueError, match="eventCount"):
        issue_to_row(issue(eventCount=None), "org", "project")


def test_issue_document_is_identical_for_every_message_order():
    from itertools import permutations

    messages = [
        {"message": "AAA payment details", "eventMessage": "Request failed"},
        {"title": "Zulu failure", "message": "Payment secret@example.test failed"},
        {"title": "Alpha failure", "eventMessage": "Request failed"},
    ]
    expected = issue_to_row(issue(messages=messages), "org", "project")
    assert expected["title"] == "Alpha failure"
    for reordered in permutations(messages):
        assert issue_to_row(issue(messages=list(reordered)), "org", "project") == expected
    assert expected["content"].count("Request failed") == 1
    assert "secret@example.test" not in expected["content"]


@pytest.mark.parametrize("field", ["title", "message", "eventMessage"])
def test_issue_title_fallback_is_deterministic(field):
    messages = [{field: "Zulu failure"}, {field: "Alpha failure"}]
    expected = issue_to_row(issue(messages=messages), "org", "project")
    assert expected["title"] == "Alpha failure"
    assert issue_to_row(issue(messages=messages[::-1]), "org", "project") == expected


def test_bounded_session_import_requests_full_window():
    client = FakeClient([{"sessionDefinitions": [session()], "messageContent": "private"}])
    rows = fetch(client, "sessions")
    assert len(rows) == 1
    name, arguments = client.calls[0]
    assert name == "find_sessions"
    assert arguments["limit"] == 5
    assert "2026-10-12T12:00:00+00:00" in arguments["query"]


def test_repeat_import_reemits_records_and_requeries_entire_window():
    client = FakeClient(
        [
            {"sessionDefinitions": [session()]},
            {"sessionDefinitions": [session()]},
        ]
    )
    assert fetch(client, "sessions") == fetch(client, "sessions")
    assert client.calls[0] == client.calls[1]


def test_limit_saturation_aborts_before_yielding():
    client = FakeClient([{"issues": [issue(), issue(url="https://example/2")]}])
    with pytest.raises(IncompleteLogRocketResultError, match="may be truncated"):
        fetch(client, "issues", limit=2)


def test_malformed_record_aborts_before_yielding_valid_records():
    client = FakeClient([{"issues": [issue(), issue(eventCount=None)]}])
    with pytest.raises(ValueError, match="eventCount"):
        next(
            _fetch_rows(
                client,
                "issues",
                organization_id="org",
                project_id="project",
                limit=5,
                lookback_days=30,
                window_start=None,
                window_end=None,
                query=None,
                include_user_id=False,
                include_location=False,
            )
        )


def test_issue_import_uses_retained_window_and_all_statuses():
    client = FakeClient([{"issues": [issue()]}])
    rows = fetch(client, "issues", query="checkout")
    assert len(rows) == 1
    name, arguments = client.calls[0]
    assert name == "find_issues"
    assert arguments["startDate"] == "2026-09-19T12:00:00+00:00"
    assert arguments["query"] == "checkout"
    assert arguments["severity"] == "any"
    assert len(arguments["triageStatuses"]) == 4


def test_old_issue_triage_change_is_refetched_next_day():
    client = FakeClient(
        [
            {"issues": [issue(latest="2026-10-01T11:00:00Z")]},
            {"issues": [issue(latest="2026-10-01T11:00:00Z", triageStatus="ignored")]},
        ]
    )
    first = fetch(client, "issues")
    second = fetch(client, "issues", now=datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert first[0]["id"] == second[0]["id"]
    assert "Triage status: ignored" in second[0]["content"]
    assert client.calls[1][1]["startDate"] == "2026-09-20T12:00:00+00:00"


def test_fixed_window_retries_do_not_skip_short_or_empty_results():
    client = FakeClient([{"sessionDefinitions": []}, {"sessionDefinitions": [session()]}])
    window = {
        "window_start": datetime(2026, 10, 1, tzinfo=UTC),
        "window_end": datetime(2026, 10, 2, tzinfo=UTC),
    }
    assert fetch(client, "sessions", **window) == []
    assert len(fetch(client, "sessions", now=datetime(2026, 10, 25, tzinfo=UTC), **window)) == 1
    assert client.calls[0] == client.calls[1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_start": datetime(2026, 10, 1)},
        {"window_end": "2026-10-02"},
        {
            "window_start": datetime(2026, 10, 3, tzinfo=UTC),
            "window_end": datetime(2026, 10, 2, tzinfo=UTC),
        },
        {"session_lookback_days": True},
        {"issue_lookback_days": 0},
        {"limit": True},
    ],
)
def test_factory_rejects_invalid_window_before_network(kwargs):
    with pytest.raises(ValueError):
        logrocket_source("org", "project", acknowledge_bounded_results=True, **kwargs)


def test_issue_messages_preserve_distinct_fields_and_redact_every_field():
    record = issue(
        messages=[
            {
                "title": "Checkout error",
                "message": "Payment secret@example.test failed",
                "eventMessage": "Bearer secret-token from 203.0.113.10",
            },
            {"message": "Checkout error"},
        ]
    )
    row = issue_to_row(record, "org", "project")
    assert row["title"] == "Checkout error"
    assert row["content"].count("Checkout error") == 1
    assert "Payment [redacted-email] failed" in row["content"]
    assert "Bearer [redacted] from [redacted-ip]" in row["content"]
    assert "secret-token" not in row["content"]


def test_factory_requires_explicit_bounded_results_acknowledgement():
    with pytest.raises(ValueError, match="acknowledge_bounded_results"):
        logrocket_source("org", "project")


def test_source_declares_document_marker_and_merge_resources():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = logrocket_source(
        "org",
        "project",
        resources=("sessions",),
        client=FakeClient([]),
        acknowledge_bounded_results=True,
    )
    assert document_source_tag(source) == "logrocket"
    assert list(source.resources) == ["logrocket_sessions"]
    assert source.resources["logrocket_sessions"].write_disposition == "merge"


def test_remember_helper_enforces_merge_at_cognee_boundary(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import cognee

    remember = AsyncMock(return_value="ingested")
    monkeypatch.setattr(cognee, "remember", remember)
    source = logrocket_source(
        "org", "project", client=FakeClient([]), acknowledge_bounded_results=True
    )
    assert asyncio.run(remember_logrocket(source, dataset_name="dedicated")) == "ingested"
    remember.assert_awaited_once_with(
        source, dataset_name="dedicated", write_disposition="merge", primary_key="id"
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"write_disposition": "replace"}, {"write_disposition": "append"}, {"primary_key": "url"}],
)
def test_remember_helper_rejects_unsafe_overrides(kwargs):
    import asyncio

    source = logrocket_source(
        "org", "project", client=FakeClient([]), acknowledge_bounded_results=True
    )
    with pytest.raises(ValueError):
        asyncio.run(remember_logrocket(source, **kwargs))


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "logrocket.db").as_posix()
    return dlt.pipeline(
        pipeline_name="logrocket_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="logrocket_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_session_pipeline(dlt, tmp_path, response):
    pipeline = _pipeline(dlt, tmp_path)
    source = logrocket_source(
        "org",
        "project",
        resources=("sessions",),
        client=FakeClient([response]),
        limit=5,
        acknowledge_bounded_results=True,
    )
    pipeline.run(source)
    return pipeline


def _stored_session_content(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM logrocket_sessions") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_dlt_pipeline_merges_updates_and_empty_result_does_not_delete(tmp_path):
    dlt = pytest.importorskip("dlt")
    pipeline = _run_session_pipeline(
        dlt, tmp_path, {"sessionDefinitions": [session(durationMs=1000)]}
    )
    stored = _stored_session_content(pipeline)
    assert len(stored) == 1
    assert "Duration milliseconds: 1000" in next(iter(stored.values()))
    resource_state = pipeline.state.get("sources", {}).get("logrocket", {}).get("resources", {})
    assert "watermark" not in json.dumps(resource_state)
    assert "content_hashes" not in json.dumps(resource_state)

    pipeline = _run_session_pipeline(
        dlt, tmp_path, {"sessionDefinitions": [session(durationMs=2000)]}
    )
    stored = _stored_session_content(pipeline)
    assert len(stored) == 1
    assert "Duration milliseconds: 2000" in next(iter(stored.values()))

    pipeline = _run_session_pipeline(dlt, tmp_path, {"sessionDefinitions": []})
    assert len(_stored_session_content(pipeline)) == 1


@pytest.mark.parametrize("disposition", ["replace", "append"])
def test_dlt_rejects_disposition_override_before_search_or_load(tmp_path, disposition):
    import dlt
    from dlt.pipeline.exceptions import PipelineStepFailed

    pipeline = _run_session_pipeline(dlt, tmp_path, {"sessionDefinitions": [session()]})
    original = _stored_session_content(pipeline)
    client = FakeClient([])
    source = logrocket_source(
        "org", "project", resources=("sessions",), client=client, acknowledge_bounded_results=True
    )
    with pytest.raises(PipelineStepFailed, match="require write_disposition"):
        pipeline.run(source, write_disposition=disposition)
    assert client.calls == []
    assert _stored_session_content(pipeline) == original
