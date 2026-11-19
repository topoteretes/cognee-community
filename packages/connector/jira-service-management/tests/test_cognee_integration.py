"""Check the connector's document seam against the installed Cognee package.

This is not a live JSM or LLM/graph end-to-end test.
"""

from types import SimpleNamespace
from uuid import uuid4

from cognee.tasks.ingestion.dlt_utils import document_source_tag
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_jira_service_management import jira_service_management_source


def test_cognee_recognizes_source_and_builds_document():
    source = jira_service_management_source(
        base_url="https://example.atlassian.net",
        service_desk_ids=["10"],
        email="test@example.com",
        api_token="test-token",
    )
    tag = document_source_tag(source)
    assert tag == "jira_service_management"
    row = SimpleNamespace(
        row_data={
            "id": "1",
            "title": "Need access",
            "url": "https://example.atlassian.net/browse/HELP-1",
            "content": "Please help\n\nResolution: ongoing, not breached",
        },
        table_name="jira_service_management_requests",
        content_hash="test-hash",
    )
    item = _build_document_data_item(row, uuid4(), tag)
    assert item.data.startswith("# Need access")
    assert "Resolution: ongoing, not breached" in item.data
    # Cognee versions expose provider attribution in external or system metadata.
    metadata = getattr(item, "system_metadata", None) or item.external_metadata
    assert metadata["source"] == tag
    assert metadata["external_id"] == "1"
