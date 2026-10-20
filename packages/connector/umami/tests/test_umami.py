import pytest
from unittest.mock import MagicMock, patch
from cognee_community_connector_umami.umami import (
    umami_source,
    _format_website_markdown,
    UMAMI_TABLE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


@pytest.fixture
def sample_website():
    return {
        "id": "site_999",
        "name": "DevDocs Portal",
        "domain": "docs.example.com",
    }


@pytest.fixture
def sample_stats():
    return {
        "pageviews": {"value": 15200},
        "visitors": {"value": 4100},
        "visits": {"value": 5200},
        "bounces": {"value": 1300},
        "totaltime": {"value": 520000},
    }


@pytest.fixture
def sample_top_pages():
    return [
        {"x": "/quickstart", "y": 4200},
        {"x": "/api/auth", "y": 2800},
        {"x": "/pricing", "y": 1400},
    ]


def test_missing_api_key_raises_error():
    """Verify that omitting the Umami API key raises ValueError."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="Umami API key required"):
            umami_source(api_key=None)


def test_website_markdown_formatting_deterministic(sample_website, sample_stats, sample_top_pages):
    """Verify Markdown rendering is deterministic, structured, and contains traffic metrics."""
    md1 = _format_website_markdown(sample_website, sample_stats, sample_top_pages)
    md2 = _format_website_markdown(sample_website, sample_stats, sample_top_pages)

    assert md1 == md2  # Determinism guarantee
    assert "# Umami Website Analytics: DevDocs Portal" in md1
    assert "- **Website ID**: site_999" in md1
    assert "- **Domain**: docs.example.com" in md1
    assert "- **Unique Visitors**: 4100" in md1
    assert "- **Total Pageviews**: 15200" in md1
    assert "- **Bounce Rate**: 25.0%" in md1
    assert "- **/quickstart**: 4200 pageviews" in md1


def test_umami_source_extracts_websites(sample_website, sample_stats, sample_top_pages):
    """Verify that umami_source runs snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_request(method, url, headers, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/websites"):
            resp.json.return_value = {"data": [sample_website]}
        elif "/stats" in url:
            resp.json.return_value = sample_stats
        elif "/metrics" in url:
            resp.json.return_value = sample_top_pages
        return resp

    mock_client.request.side_effect = mock_request

    source = umami_source(api_key="umami_test_key", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "umami"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == UMAMI_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 1
    assert items[0]["id"] == "website_site_999"
    assert items[0]["name"] == "DevDocs Portal"
    assert items[0]["domain"] == "docs.example.com"
    assert items[0]["type"] == "analytics_report"
    assert "# Umami Website Analytics:" in items[0]["body"]
