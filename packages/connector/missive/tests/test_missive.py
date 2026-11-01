from typing import Any

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_missive.missive import (
    MISSIVE_SOURCE_NAME,
    MISSIVE_TABLE_NAME,
    MissiveAPIClient,
    _conversation_to_row,
    _iter_conversations,
    clean_html,
    format_timestamp,
    missive_source,
    render_conversation_content,
)


class FakeMissiveClient:
    def __init__(
        self,
        conversations: list[dict[str, Any]] | None = None,
        messages_by_id: dict[str, list[dict[str, Any]]] | None = None,
        comments_by_id: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self._conversations = conversations or []
        self._messages_by_id = messages_by_id or {}
        self._comments_by_id = comments_by_id or {}

    def get_conversations(
        self,
        limit: int = 50,
        until: int | None = None,
        mailbox: str | None = None,
        team: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        results = []
        for conv in self._conversations:
            if mailbox and conv.get("mailbox_id") != mailbox:
                continue
            if team and conv.get("team_id") != team:
                continue
            if label and label not in conv.get("label_ids", []):
                continue
            if until and conv.get("last_activity_at", 0) >= until:
                continue
            results.append(conv)
            if len(results) >= limit:
                break
        return {"conversations": results, "meta": {}}

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._messages_by_id.get(conversation_id, [])

    def get_comments(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._comments_by_id.get(conversation_id, [])


def test_missing_token_raises_error(monkeypatch):
    monkeypatch.delenv("MISSIVE_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Missive API token required"):
        missive_source(api_token=None, client=None)


def test_clean_html():
    raw_html = (
        "<h1>Important Update</h1>\n"
        "<p>Hello team,<br>Check the <a href='https://example.com'>link</a>.</p>\n"
        "<ul><li>Item 1</li><li>Item 2</li></ul>\n"
        "<p>&gt; quoted text from previous email</p>\n"
        "<p>On Mon, Oct 5, 2026 at 10:00 AM Alice wrote:</p>\n"
        "<p>This was in the old thread</p>\n"
        "<p>---</p>\n"
        "<p>Sent from my phone</p>"
    )
    cleaned = clean_html(raw_html)
    assert "# Important Update" in cleaned
    assert "Hello team" in cleaned
    assert "[link](https://example.com)" in cleaned
    assert "- Item 1" in cleaned
    assert "- Item 2" in cleaned
    assert "> quoted text" not in cleaned
    assert "Alice wrote:" not in cleaned


def test_format_timestamp():
    ts = 1760000000
    formatted = format_timestamp(ts)
    assert "UTC" in formatted
    assert format_timestamp(None) == ""


def test_render_conversation_content():
    conversation = {"id": "conv-1", "subject": "Contract Inquiry"}
    messages = [
        {
            "id": "msg-1",
            "type": "email",
            "delivered_at": 1760000000,
            "from_field": {"name": "Customer Carol", "address": "carol@example.com"},
            "body": "<p>When will the contract be signed?</p>",
        }
    ]
    comments = [
        {
            "id": "com-1",
            "created_at": 1760000100,
            "author": {"name": "Legal Dave", "email": "dave@example.com"},
            "body": "<p>Reviewed and ready for signature tomorrow.</p>",
        }
    ]

    rendered = render_conversation_content(
        conversation=conversation,
        messages=messages,
        comments=comments,
        include_comments=True,
        include_contact_details=True,
    )

    assert "## Subject: Contract Inquiry" in rendered
    assert "Email from Customer Carol <carol@example.com>" in rendered
    assert "When will the contract be signed?" in rendered
    assert "Internal Comment from Legal Dave <dave@example.com>" in rendered
    assert "Reviewed and ready for signature tomorrow." in rendered


def test_render_conversation_without_comments():
    conversation = {"id": "conv-1", "subject": "Billing issue"}
    messages = [
        {
            "id": "msg-1",
            "type": "email",
            "delivered_at": 1760000000,
            "from_field": {"name": "Bob"},
            "body": "<p>Need invoice copy</p>",
        }
    ]
    comments = [
        {
            "id": "com-1",
            "created_at": 1760000100,
            "author": {"name": "Finance Alice"},
            "body": "<p>Sent via portal</p>",
        }
    ]

    rendered = render_conversation_content(
        conversation=conversation,
        messages=messages,
        comments=comments,
        include_comments=False,
    )

    assert "Need invoice copy" in rendered
    assert "Finance Alice" not in rendered


def test_conversation_to_row():
    client = FakeMissiveClient(
        messages_by_id={
            "conv-1": [{"id": "m1", "body": "Support question", "created_at": 100}]
        },
        comments_by_id={"conv-1": []},
    )
    conv = {"id": "conv-1", "subject": "Need Help"}
    row = _conversation_to_row(client, conv)

    assert row["id"] == "conv-1"
    assert row["title"] == "Need Help"
    assert "Support question" in row["content"]
    assert row["url"] == "https://mail.missiveapp.com/#/conversations/conv-1"


def test_iter_conversations_incremental_cursor():
    conversations = [
        {"id": "c1", "last_activity_at": 1000},
        {"id": "c2", "last_activity_at": 500},
        {"id": "c3", "last_activity_at": 200},
    ]
    client = FakeMissiveClient(conversations=conversations)

    synced = list(_iter_conversations(client, since=400))
    ids = [c["id"] for c in synced]
    assert "c1" in ids
    assert "c2" in ids
    assert "c3" not in ids


def test_iter_conversations_excludes_trashed_and_junked():
    conversations = [
        {"id": "active-1", "last_activity_at": 1000},
        {"id": "trashed-1", "last_activity_at": 900, "trashed_at": 950},
        {"id": "junked-1", "last_activity_at": 800, "junked_at": 850},
    ]
    client = FakeMissiveClient(conversations=conversations)

    synced = list(_iter_conversations(client))
    ids = [c["id"] for c in synced]
    assert "active-1" in ids
    assert "trashed-1" not in ids
    assert "junked-1" not in ids


def test_missive_source_dlt_structure():
    client = FakeMissiveClient(
        conversations=[{"id": "c1", "subject": "Hello", "last_activity_at": 100}],
        messages_by_id={"c1": [{"id": "m1", "body": "World", "created_at": 100}]},
        comments_by_id={"c1": []},
    )

    source = missive_source(client=client)

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == MISSIVE_SOURCE_NAME
    assert MISSIVE_TABLE_NAME in source.resources
    resource = source.resources[MISSIVE_TABLE_NAME]
    assert resource.write_disposition == "replace"

    rows = list(resource)
    assert len(rows) == 1
    assert rows[0]["id"] == "c1"
    assert rows[0]["title"] == "Hello"
    assert "World" in rows[0]["content"]


def test_missive_api_client_retry_on_429():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"conversations": [], "meta": {}})

    transport = httpx.MockTransport(handler)
    mock_http_client = httpx.Client(
        transport=transport, base_url="https://api.missiveapp.com/v1"
    )
    client = MissiveAPIClient(api_token="test_token", client=mock_http_client)

    result = client.get_conversations()
    assert result == {"conversations": [], "meta": {}}
    assert calls == 2
