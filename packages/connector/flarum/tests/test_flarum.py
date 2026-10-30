"""Hermetic unit tests for Flarum connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_flarum.flarum import (
    FlarumClient,
    _discussion_to_document,
    _html_to_clean_text,
    flarum_source,
)

SAMPLE_DISCUSSION: Dict[str, Any] = {
    "type": "discussions",
    "id": "101",
    "attributes": {
        "title": "How to deploy Flarum on Kubernetes?",
        "slug": "how-to-deploy-flarum-on-kubernetes",
        "commentCount": 2,
        "createdAt": "2026-10-20T10:00:00Z",
        "lastPostedAt": "2026-10-20T12:30:00Z",
    },
}

SAMPLE_POSTS: List[Dict[str, Any]] = [
    {
        "type": "posts",
        "id": "501",
        "attributes": {
            "contentHtml": "<p>Has anyone successfully deployed <strong>Flarum v2</strong> on k8s using Helm?</p>",
            "createdAt": "2026-10-20T10:00:00Z",
        },
    },
    {
        "type": "posts",
        "id": "502",
        "attributes": {
            "contentHtml": "<p>Yes, make sure your persistent volume mounts <code>/public/assets</code> and <code>/storage</code> properly.</p>",
            "createdAt": "2026-10-20T12:30:00Z",
        },
    },
]


class FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class FakeFlarumHttpClient:
    def __init__(self, discussions: List[Dict[str, Any]], posts: List[Dict[str, Any]]) -> None:
        self.discussions = discussions
        self.posts = posts

    def get(self, url: str, params: Any = None) -> FakeResponse:
        if "/api/discussions" in url:
            return FakeResponse({"data": self.discussions})
        if "/api/posts" in url:
            return FakeResponse({"data": self.posts})
        return FakeResponse({}, status_code=404)


def test_html_to_clean_text():
    """Test stripping HTML tags and entities."""
    html_sample = "<p>Welcome to <strong>Flarum</strong>!<br>Check out our <a href='https://flarum.org'>website</a>.</p>"
    clean = _html_to_clean_text(html_sample)
    assert "Welcome to" in clean
    assert "Flarum" in clean
    assert "<p>" not in clean
    assert "<strong>" not in clean


def test_discussion_to_document():
    """Test converting a discussion and post thread into a Markdown document."""
    doc = _discussion_to_document(SAMPLE_DISCUSSION, posts=SAMPLE_POSTS, base_url="https://discuss.example.org")
    assert doc is not None
    assert doc["id"] == "flarum_disc_101"
    assert doc["title"] == "Discussion: How to deploy Flarum on Kubernetes?"
    assert "https://discuss.example.org/d/101-how-to-deploy-flarum-on-kubernetes" in doc["url"]

    content = doc["content"]
    assert "# Forum Discussion: How to deploy Flarum on Kubernetes?" in content
    assert "Has anyone successfully deployed Flarum v2 on k8s using Helm?" in content
    assert "persistent volume mounts /public/assets and /storage" in content
    assert "**Total Replies:** 2" in content


def test_flarum_source_dlt_execution():
    """Test executing flarum_source with mocked HTTP client."""
    fake_client = FakeFlarumHttpClient(discussions=[SAMPLE_DISCUSSION], posts=SAMPLE_POSTS)
    client = FlarumClient(base_url="https://discuss.example.org", http_client=fake_client)

    resource = flarum_source(base_url="https://discuss.example.org", client=client)
    items = list(resource)

    assert len(items) == 1
    assert items[0]["id"] == "flarum_disc_101"
    assert "How to deploy Flarum on Kubernetes?" in items[0]["title"]
