import sys
import types

import pytest

from cognee_community_connector_raindrop.raindrop import (
    RaindropAPIError,
    get_bookmarks,
    normalize_bookmark,
    raindrop_source,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}" if status_code < 400 else b"{\"error\":{\"message\":\"failed\"}}"

    def json(self):
        return self.payload


def test_normalize_bookmark_extracts_key_fields():
    bookmark = {
        "_id": 12,
        "title": "Memory for AI Agents",
        "excerpt": "A good overview",
        "link": "https://example.com/ai-memory",
        "tags": [{"title": "ai"}, {"title": "memory"}],
        "collection": {"_id": 55, "title": "Research"},
        "created": "2024-01-10T12:00:00Z",
        "updated": "2024-02-01T15:00:00Z",
    }

    row = normalize_bookmark(bookmark)

    assert row["id"] == 12
    assert row["title"] == "Memory for AI Agents"
    assert row["excerpt"] == "A good overview"
    assert row["url"] == "https://example.com/ai-memory"
    assert row["tags"] == "ai, memory"
    assert row["collection"] == "Research"
    assert row["created_at"] == "2024-01-10T12:00:00Z"
    assert row["updated_at"] == "2024-02-01T15:00:00Z"


def test_get_bookmarks_handles_paginated_result(monkeypatch):
    calls = {"count": 0}

    def fake_get(url, headers=None, params=None, timeout=30):
        calls["count"] += 1
        if calls["count"] == 1:
            return FakeResponse({
                "items": [{
                    "_id": 1,
                    "title": "One",
                    "link": "https://a",
                    "excerpt": "A",
                    "tags": [{"title": "alpha"}],
                    "collection": {"title": "Inbox"},
                }]
            })
        if calls["count"] == 2:
            return FakeResponse({
                "items": [{
                    "_id": 2,
                    "title": "Two",
                    "link": "https://b",
                    "excerpt": "B",
                    "tags": [{"title": "beta"}, {"title": "gamma"}],
                    "collection": {"title": "Saved"},
                }]
            })
        return FakeResponse({"items": []})

    monkeypatch.setenv("RAINDROP_API_TOKEN", "demo-token")
    monkeypatch.setattr("cognee_community_connector_raindrop.raindrop.requests.get", fake_get)

    rows = get_bookmarks(page_size=1)

    assert len(rows) == 2
    assert rows[0]["title"] == "One"
    assert rows[1]["tags"] == "beta, gamma"
    assert rows[1]["collection"] == "Saved"


def test_raindrop_source_requires_api_token(monkeypatch):
    monkeypatch.delenv("RAINDROP_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="RAINDROP_API_TOKEN"):
        raindrop_source()


def test_raindrop_source_calls_dlt_wrappers(monkeypatch):
    fake_dlt = types.ModuleType("dlt")
    fake_dlt.resource = lambda **kwargs: (lambda func: func)
    fake_dlt.source = lambda **kwargs: (lambda func: func)
    monkeypatch.setitem(sys.modules, "dlt", fake_dlt)
    monkeypatch.setenv("RAINDROP_API_TOKEN", "demo-token")

    def fake_get(url, headers=None, params=None, timeout=30):
        return FakeResponse({
            "items": [{
                "_id": 7,
                "title": "Docs",
                "link": "https://docs.example",
                "excerpt": "Notes",
                "tags": [{"title": "docs"}],
                "collection": {"title": "Work"},
            }]
        })

    monkeypatch.setattr("cognee_community_connector_raindrop.raindrop.requests.get", fake_get)

    source = raindrop_source()
    rows = list(source())

    assert len(rows) == 1
    assert rows[0]["title"] == "Docs"
    assert rows[0]["collection"] == "Work"


def test_get_bookmarks_raises_api_error_on_http_failure(monkeypatch):
    monkeypatch.setenv("RAINDROP_API_TOKEN", "demo-token")

    def fake_get(url, headers=None, params=None, timeout=30):
        return FakeResponse({"error": {"message": "bad token"}}, status_code=401)

    monkeypatch.setattr("cognee_community_connector_raindrop.raindrop.requests.get", fake_get)

    with pytest.raises(RaindropAPIError):
        get_bookmarks()
