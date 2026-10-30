from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_postmark.postmark import (
    DOCUMENT_SOURCE_ATTR,
    POSTMARK_TABLE_NAME,
    _format_template_markdown,
    _strip_html_tags,
    postmark_source,
)


@pytest.fixture
def sample_template():
    return {
        "TemplateId": 12345,
        "Name": "Customer Password Reset",
        "Alias": "password-reset",
        "TemplateType": "Standard",
        "Subject": "Reset your password for {{app_name}}",
        "LayoutTemplate": "clean-layout",
        "TextBody": "Click the following link to reset your password: {{reset_url}}",
        "HtmlBody": (
            "<p>Click the following link to reset your password: "
            "<a href='{{reset_url}}'>Reset</a></p>"
        ),
    }


def test_missing_server_token_raises_error():
    """Verify that omitting the Postmark Server Token raises ValueError."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="Postmark Server API Token required"),
    ):
        postmark_source(server_token=None)


def test_strip_html_tags_clean_output():
    """Verify that HTML email tags and styles are stripped cleanly."""
    html = (
        "<style>.btn{color:blue;}</style><h1>Invoice Receipt</h1>"
        "<p>Amount: <strong>$50.00</strong></p>"
    )
    clean = _strip_html_tags(html)
    assert clean == "Invoice Receipt Amount: $50.00"


def test_template_markdown_formatting_deterministic(sample_template):
    """Verify Markdown rendering is deterministic, structured, and contains body content."""
    md1 = _format_template_markdown(sample_template)
    md2 = _format_template_markdown(sample_template)

    assert md1 == md2  # Determinism guarantee
    assert "# Postmark Template: Customer Password Reset" in md1
    assert "- **Template ID**: 12345" in md1
    assert "- **Alias**: password-reset" in md1
    assert "- **Subject Line**: Reset your password for {{app_name}}" in md1
    assert "Click the following link to reset your password: {{reset_url}}" in md1


def test_postmark_source_extracts_templates(sample_template):
    """Verify that postmark_source runs snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_request(method, url, headers, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/templates"):
            resp.json.return_value = {
                "TotalCount": 1,
                "Templates": [
                    {
                        "TemplateId": 12345,
                        "Name": "Customer Password Reset",
                        "Alias": "password-reset",
                    }
                ],
            }
        elif "/templates/12345" in url:
            resp.json.return_value = sample_template
        return resp

    mock_client.request.side_effect = mock_request

    source = postmark_source(server_token="test_server_token", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "postmark"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == POSTMARK_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 1
    assert items[0]["id"] == "template_12345"
    assert items[0]["name"] == "Customer Password Reset"
    assert items[0]["alias"] == "password-reset"
    assert items[0]["type"] == "email_template"
    assert "# Postmark Template:" in items[0]["body"]
