from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_dub.dub import (
    DOCUMENT_SOURCE_ATTR,
    DUB_TABLE_NAME,
    _format_link_markdown,
    dub_source,
)


@pytest.fixture
def sample_link():
    return {
        "id": "link_xyz123",
        "domain": "dub.sh",
        "key": "product-launch",
        "shortLink": "https://dub.sh/product-launch",
        "url": "https://company.com/blog/2026-product-announcement?utm_source=twitter",
        "title": "2026 Product Announcement",
        "description": "Main announcement link for Twitter and LinkedIn release",
        "clicks": 1420,
        "tags": [{"name": "launch"}, {"name": "q4"}],
        "comments": "Pinned link on company profile",
    }


def test_missing_api_key_raises_error():
    """Verify that omitting the Dub.co API token raises ValueError."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match=r"Dub\.co API token required"),
    ):
        dub_source(api_key=None)


def test_link_markdown_formatting_deterministic(sample_link):
    """Verify Markdown rendering is deterministic, structured, and contains redirect info."""
    md1 = _format_link_markdown(sample_link)
    md2 = _format_link_markdown(sample_link)

    assert md1 == md2  # Determinism guarantee
    assert "# Dub.co Link: 2026 Product Announcement" in md1
    assert "- **Link ID**: link_xyz123" in md1
    assert "- **Short URL**: https://dub.sh/product-launch" in md1
    assert "- **Destination URL**: https://company.com/blog/2026-product-announcement" in md1
    assert "- **Total Clicks**: 1420" in md1
    assert "launch, q4" in md1
    assert "Pinned link on company profile" in md1


def test_dub_source_extracts_links(sample_link):
    """Verify that dub_source runs snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_request(method, url, headers, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/workspaces"):
            resp.json.return_value = [{"id": "ws_1", "name": "Acme Marketing"}]
        elif "/links" in url:
            if params and params.get("page", 1) == 1:
                resp.json.return_value = [sample_link]
            else:
                resp.json.return_value = []
        return resp

    mock_client.request.side_effect = mock_request

    source = dub_source(api_key="dub_test_token", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "dub"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == DUB_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 1
    assert items[0]["id"] == "link_link_xyz123"
    assert items[0]["key"] == "product-launch"
    assert (
        items[0]["url"] == "https://company.com/blog/2026-product-announcement?utm_source=twitter"
    )
    assert items[0]["type"] == "short_link"
    assert "# Dub.co Link:" in items[0]["body"]
