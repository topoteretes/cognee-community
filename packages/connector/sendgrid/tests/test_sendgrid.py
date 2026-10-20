import pytest
from unittest.mock import MagicMock, patch
from cognee_community_connector_sendgrid.sendgrid import (
    sendgrid_source,
    _format_template_markdown,
    _format_marketing_list_markdown,
    _strip_html_tags,
    SENDGRID_TABLE_NAME,
    DOCUMENT_SOURCE_ATTR,
)


@pytest.fixture
def sample_template():
    return {
        "id": "d-abc123456789",
        "name": "User Welcome & Onboarding",
        "generation": "dynamic",
        "updated_at": "2026-10-15 10:00:00",
        "versions": [
            {
                "id": "v101",
                "name": "Version 1.0",
                "active": 1,
                "subject": "Welcome to our platform, {{first_name}}!",
                "editor": "code",
                "plain_content": "Thanks for signing up! Here is your quickstart guide to get started.",
                "html_content": "<p>Thanks for signing up! Here is your quickstart guide to get started.</p>",
            }
        ],
    }


@pytest.fixture
def sample_marketing_list():
    return {
        "id": "ml-9988",
        "name": "Active Beta Testers",
        "contact_count": 450,
    }


def test_missing_api_key_raises_error():
    """Verify that omitting the SendGrid API key raises ValueError."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="SendGrid API key required"):
            sendgrid_source(api_key=None)


def test_strip_html_tags_clean_output():
    """Verify that HTML email tags and styles are stripped cleanly."""
    html = "<style>.btn{color:red;}</style><h2>Hello!</h2><p>Click <a href='#'>here</a>.</p>"
    clean = _strip_html_tags(html)
    assert clean == "Hello! Click here ."


def test_template_markdown_formatting_deterministic(sample_template):
    """Verify Markdown rendering is deterministic, structured, and contains active version content."""
    md1 = _format_template_markdown(sample_template)
    md2 = _format_template_markdown(sample_template)

    assert md1 == md2  # Determinism guarantee
    assert "# SendGrid Template: User Welcome & Onboarding" in md1
    assert "- **Template ID**: d-abc123456789" in md1
    assert "- **Generation**: dynamic" in md1
    assert "- **Subject Line**: Welcome to our platform, {{first_name}}!" in md1
    assert "Thanks for signing up! Here is your quickstart guide" in md1


def test_marketing_list_markdown_formatting(sample_marketing_list):
    """Verify marketing list formatting."""
    md = _format_marketing_list_markdown(sample_marketing_list)
    assert "# SendGrid Marketing List: Active Beta Testers" in md
    assert "- **List ID**: ml-9988" in md
    assert "- **Total Contacts**: 450" in md


def test_sendgrid_source_extracts_templates_and_lists(sample_template, sample_marketing_list):
    """Verify that sendgrid_source runs snapshot extraction with document-mode attribute."""
    mock_client = MagicMock()

    def mock_request(method, url, headers, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/templates?generations=dynamic,legacy&page_size=100"):
            resp.json.return_value = {"result": [{"id": "d-abc123456789", "name": "User Welcome & Onboarding"}]}
        elif "/templates/d-abc123456789" in url:
            resp.json.return_value = sample_template
        elif "/marketing/lists" in url:
            resp.json.return_value = {"result": [sample_marketing_list]}
        return resp

    mock_client.request.side_effect = mock_request

    source = sendgrid_source(api_key="SG.test_key", client=mock_client)

    # Verify document-mode attribute is set for entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "sendgrid"

    # Evaluate the resource items
    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == SENDGRID_TABLE_NAME
    assert resource.write_disposition == "replace"

    items = list(resource())
    assert len(items) == 2
    assert items[0]["id"] == "template_d-abc123456789"
    assert items[0]["name"] == "User Welcome & Onboarding"
    assert items[0]["type"] == "email_template"
    assert items[1]["id"] == "list_ml-9988"
    assert items[1]["name"] == "Active Beta Testers"
    assert items[1]["type"] == "marketing_list"
