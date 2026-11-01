"""Hermetic unit tests for Drupal connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_drupal.drupal import (
    DrupalClient,
    _html_to_clean_text,
    _node_to_document,
    drupal_source,
)

SAMPLE_ARTICLE_NODE: Dict[str, Any] = {
    "type": "node--article",
    "id": "e7c1c2e4-9842-4f3b-8219-4820875cfa89",
    "attributes": {
        "drupal_internal__nid": 42,
        "title": "Corporate Remote Work & Travel Policy 2026",
        "created": "2026-01-15T09:00:00+00:00",
        "changed": "2026-10-22T14:30:00+00:00",
        "path": {"alias": "/policies/remote-work-2026"},
        "body": {
            "value": "<p>Employees may work remotely up to <strong>100%</strong> of the time with manager approval.</p><p>Hardware requests must be submitted through the IT service desk.</p>",
            "summary": "Guidelines for global remote work and hardware allowance.",
        },
    },
}

SAMPLE_PAGE_NODE: Dict[str, Any] = {
    "type": "node--page",
    "id": "a1b2c3d4-0000-1111-2222-333344445555",
    "attributes": {
        "drupal_internal__nid": 10,
        "title": "About Our Engineering Culture",
        "created": "2026-02-01T10:00:00+00:00",
        "changed": "2026-08-10T11:00:00+00:00",
        "path": {"alias": "/about/engineering"},
        "body": {
            "value": "<p>We build open-source tools with rigorous testing and continuous integration.</p>",
            "summary": "Engineering team mission and principles.",
        },
    },
}


class FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Any:
        return self._json_data


class FakeDrupalHttpClient:
    def __init__(self, article_nodes: List[Dict[str, Any]], page_nodes: List[Dict[str, Any]]) -> None:
        self.article_nodes = article_nodes
        self.page_nodes = page_nodes

    def get(self, url: str, params: Any = None) -> FakeResponse:
        if "/jsonapi/node/article" in url:
            return FakeResponse({"data": self.article_nodes})
        if "/jsonapi/node/page" in url:
            return FakeResponse({"data": self.page_nodes})
        return FakeResponse({}, status_code=404)


def test_html_to_clean_text():
    """Test stripping HTML tags and entities."""
    html_sample = "<p>Drupal <strong>JSON:API</strong> integration works <em>flawlessly</em>!</p>"
    clean = _html_to_clean_text(html_sample)
    assert clean == "Drupal JSON:API integration works flawlessly!"


def test_node_to_document():
    """Test converting a Drupal JSON:API article node into a Markdown document."""
    doc = _node_to_document(SAMPLE_ARTICLE_NODE, bundle="article", base_url="https://cms.company.com")
    assert doc is not None
    assert doc["id"] == "drupal_article_e7c1c2e4-9842-4f3b-8219-4820875cfa89"
    assert doc["title"] == "[Article] Corporate Remote Work & Travel Policy 2026"
    assert doc["url"] == "https://cms.company.com/policies/remote-work-2026"

    content = doc["content"]
    assert "# Drupal Article: Corporate Remote Work & Travel Policy 2026" in content
    assert "Guidelines for global remote work and hardware allowance." in content
    assert "Employees may work remotely up to 100% of the time" in content
    assert "https://cms.company.com/policies/remote-work-2026" in content


def test_drupal_source_dlt_execution():
    """Test full DLT source execution with mocked Drupal client across multiple bundles."""
    fake_client = FakeDrupalHttpClient(article_nodes=[SAMPLE_ARTICLE_NODE], page_nodes=[SAMPLE_PAGE_NODE])
    client = DrupalClient(base_url="https://cms.company.com", http_client=fake_client)

    resource = drupal_source(base_url="https://cms.company.com", bundles=["article", "page"], client=client)
    items = list(resource)

    # 1 article + 1 page = 2 items
    assert len(items) == 2
    assert items[0]["id"] == "drupal_article_e7c1c2e4-9842-4f3b-8219-4820875cfa89"
    assert items[1]["id"] == "drupal_page_a1b2c3d4-0000-1111-2222-333344445555"
