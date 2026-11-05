"""Offline unit test suite for cognee-community-connector-backlog (#6453).

Tests cover:
1. Space URL validation (`backlog.com` and `backlog.jp`)
2. Strict `apiKey` redaction across URLs, exceptions, and ingested documents
3. PII protection (assignee & commenter emails stripped; display names only)
4. Issue & ordered comment thread ingestion
5. `count` + `offset` pagination
6. Incremental `updatedSince` cursor & selective comment fetching
7. Forget-on-delete `_deleted` tombstones & network error safety abort
8. Rate-limit header parsing (`X-RateLimit-*`) and HTTP 429 backoff
9. Backlog `/api/v2/documents` ingestion
"""

from typing import Any
import pytest

from cognee_community_connector_backlog import (
    DOCUMENT_SOURCE_ATTR,
    backlog_source,
    parse_backlog_document,
    parse_backlog_issue,
    parse_rate_limit_headers,
    redact_api_key,
    sanitize_user_display_name,
    sync_backlog_projects,
    validate_space_url,
)


def test_space_url_validation_backlog_com_and_jp() -> None:
    """Verify .backlog.com and .backlog.jp HTTPS space URLs pass and invalid URLs raise."""
    assert validate_space_url("https://acme.backlog.com/") == "https://acme.backlog.com"
    assert validate_space_url("https://team.backlog.jp") == "https://team.backlog.jp"

    with pytest.raises(ValueError, match="HTTPS"):
        validate_space_url("http://acme.backlog.com")

    with pytest.raises(ValueError, match="Backlog domain"):
        validate_space_url("https://evil.example.com")


def test_api_key_redaction_in_urls_errors_and_documents() -> None:
    """Verify the API key never appears in logs, error messages, or stored documents."""
    secret_key = "SUPER_SECRET_BACKLOG_KEY_999"
    raw_url = f"https://acme.backlog.com/api/v2/issues?projectId[]=1&apiKey={secret_key}&count=100"
    redacted_url = redact_api_key(raw_url, secret_key)
    assert secret_key not in redacted_url
    assert "apiKey=[REDACTED]" in redacted_url

    raw_issue = {
        "id": 101,
        "issueKey": "OPS-1",
        "summary": f"Investigate URL {raw_url}",
        "description": f"Token leaked: {secret_key}",
        "updated": "2026-10-01T10:00:00Z",
    }
    parsed = parse_backlog_issue(raw_issue, api_key=secret_key)
    row = parsed.to_document_row()
    assert secret_key not in row["summary"]
    assert secret_key not in row["text"]
    assert "[REDACTED]" in row["text"]


def test_pii_email_stripping_keeps_only_display_names() -> None:
    """Verify assignee and commenter emails are never ingested into graph documents."""
    user_with_email = {
        "id": 55,
        "name": "Kenji Sato (kenji@example.jp)",
        "mailAddress": "kenji@example.jp",
    }
    clean_name = sanitize_user_display_name(user_with_email)
    assert "kenji@example.jp" not in clean_name
    assert "Kenji Sato" in clean_name


def test_issue_and_ordered_comments_ingestion() -> None:
    """Verify issues and their comments are ingested in chronological order."""
    raw_issue = {
        "id": 200,
        "issueKey": "PAY-42",
        "summary": "Switch payment retry to 3 attempts",
        "description": "Retry backoff adjusted for gateway stability.",
        "issueType": {"name": "Bug"},
        "status": {"name": "Resolved"},
        "priority": {"name": "High"},
        "assignee": {"name": "Aiko Tanaka", "mailAddress": "aiko@corp.jp"},
        "milestone": [{"name": "v2.4"}],
        "updated": "2026-10-12T12:00:00Z",
    }
    # Intentionally out of order to verify chronological sorting
    raw_comments = [
        {
            "id": 2,
            "content": "Confirmed 3 attempts fixed the timeout.",
            "created": "2026-10-12T11:30:00Z",
            "createdUser": {"name": "Taro", "mailAddress": "taro@corp.jp"},
        },
        {
            "id": 1,
            "content": "Initial retry count of 5 caused queue saturation.",
            "created": "2026-10-12T09:15:00Z",
            "createdUser": {"name": "Aiko Tanaka", "mailAddress": "aiko@corp.jp"},
        },
    ]

    record = parse_backlog_issue(raw_issue, raw_comments)
    row = record.to_document_row()
    assert row["id"] == "backlog:issue:PAY-42"
    assert row["_deleted"] is False
    assert "aiko@corp.jp" not in row["text"]
    assert "taro@corp.jp" not in row["text"]
    idx_first = row["text"].index("Initial retry count of 5")
    idx_second = row["text"].index("Confirmed 3 attempts fixed")
    assert idx_first < idx_second


def test_count_and_offset_pagination_and_incremental_cursor() -> None:
    """Verify count+offset paging, updatedSince cursor, and selective comment fetching."""
    comment_calls: list[str] = []

    def fake_fetch(url: str, params: dict[str, Any]) -> Any:
        if url.endswith("/api/v2/projects"):
            return [{"id": 1, "projectKey": "CORE"}]
        if url.endswith("/api/v2/issues"):
            offset = int(params.get("offset", 0))
            updated_since = params.get("updatedSince")
            if updated_since == "2026-10-20T00:00:00Z":
                if offset == 0:
                    return [
                        {
                            "id": 2,
                            "issueKey": "CORE-2",
                            "summary": "Updated issue 2",
                            "updated": "2026-10-25T00:00:00Z",
                        }
                    ]
                return []
            all_items = [
                {
                    "id": 1,
                    "issueKey": "CORE-1",
                    "summary": "Issue 1",
                    "updated": "2026-10-10T00:00:00Z",
                },
                {
                    "id": 2,
                    "issueKey": "CORE-2",
                    "summary": "Issue 2",
                    "updated": "2026-10-20T00:00:00Z",
                },
            ]
            count = int(params.get("count", 100))
            return all_items[offset : offset + count]
        if "/comments" in url:
            comment_calls.append(url)
            return [
                {
                    "id": 10,
                    "content": "Note",
                    "created": "2026-10-10T01:00:00Z",
                    "createdUser": {"name": "Dev"},
                }
            ]
        return []

    state: dict[str, Any] = {}
    rows_run1 = sync_backlog_projects(
        space_url="https://acme.backlog.com",
        api_key="KEY123",
        fetch_json_fn=fake_fetch,
        state_dict=state,
        page_size=1,
    )
    assert len(rows_run1) == 2
    assert state["cursors"]["CORE"] == "2026-10-20T00:00:00Z"
    assert len(comment_calls) == 2

    comment_calls.clear()
    rows_run2 = sync_backlog_projects(
        space_url="https://acme.backlog.com",
        api_key="KEY123",
        fetch_json_fn=fake_fetch,
        state_dict=state,
        page_size=10,
    )
    assert len(rows_run2) == 1
    assert rows_run2[0]["issue_key"] == "CORE-2"
    assert len(comment_calls) == 1
    assert "CORE-2/comments" in comment_calls[0]
    assert state["cursors"]["CORE"] == "2026-10-25T00:00:00Z"


def test_forget_on_delete_tombstones_and_network_error_safety_abort() -> None:
    """Verify deleted issues emit _deleted=True tombstones and network errors abort safely."""
    state: dict[str, Any] = {
        "cursors": {"CORE": "2026-10-20T00:00:00Z"},
        "ingested_issues": {"CORE": {1: "CORE-1", 2: "CORE-2"}},
    }

    def fake_fetch_deleted(url: str, params: dict[str, Any]) -> Any:
        if url.endswith("/api/v2/projects"):
            return [{"id": 1, "projectKey": "CORE"}]
        if url.endswith("/api/v2/issues"):
            if "updatedSince" in params:
                return []
            return [{"id": 1, "issueKey": "CORE-1"}]
        return []

    rows = sync_backlog_projects(
        space_url="https://acme.backlog.com",
        api_key="SECRET_TOKEN_ABC",
        fetch_json_fn=fake_fetch_deleted,
        state_dict=state,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "backlog:issue:CORE-2"
    assert rows[0]["_deleted"] is True
    assert 2 not in state["ingested_issues"]["CORE"]

    # Scenario B: Network error during live sweep aborts without deleting CORE-1
    def fake_fetch_network_fail(url: str, params: dict[str, Any]) -> Any:
        if url.endswith("/api/v2/projects"):
            return [{"id": 1, "projectKey": "CORE"}]
        if url.endswith("/api/v2/issues"):
            if "updatedSince" in params:
                return []
            err_url = "https://acme.backlog.com/api/v2/issues?apiKey=SECRET_TOKEN_ABC"
            raise ConnectionError(f"HTTP 502 on {err_url}")
        return []

    with pytest.raises(RuntimeError) as exc_info:
        sync_backlog_projects(
            space_url="https://acme.backlog.com",
            api_key="SECRET_TOKEN_ABC",
            fetch_json_fn=fake_fetch_network_fail,
            state_dict=state,
        )
    assert "SECRET_TOKEN_ABC" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert 1 in state["ingested_issues"]["CORE"]


def test_rate_limit_header_parsing_and_429_backoff() -> None:
    """Verify rate-limit headers and 429 retry wait calculation."""
    headers = {
        "X-RateLimit-Limit": "60",
        "X-RateLimit-Remaining": "3",
        "X-RateLimit-Reset": "1700000030",
    }
    rl = parse_rate_limit_headers(headers)
    assert rl.limit == 60
    assert rl.remaining == 3
    assert rl.should_throttle(low_watermark=5) is True
    assert rl.compute_backoff_seconds(status_code=429, now_epoch=1700000010.0) == 20.0
    assert rl.compute_backoff_seconds(status_code=200, now_epoch=1700000010.0) > 0.0


def test_backlog_documents_and_dlt_source_attribute() -> None:
    """Verify /api/v2/documents parsing and DOCUMENT_SOURCE_ATTR marker on backlog_source."""
    assert getattr(backlog_source, DOCUMENT_SOURCE_ATTR, False) is True
    raw_doc = {
        "id": "doc-77",
        "title": "Runbook",
        "plain": "Restart worker on OOM.",
        "updated": "2026-10-30T00:00:00Z",
    }
    doc = parse_backlog_document(raw_doc, project_key="OPS")
    row = doc.to_document_row()
    assert row["id"] == "backlog:doc:OPS:doc-77"
    assert "Restart worker on OOM." in row["text"]
    assert row["_deleted"] is False
