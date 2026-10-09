import pytest
from unittest.mock import patch, MagicMock

from cognee_community_connector_linear.linear import LINEAR_SOURCE_NAME, linear_source

def test_source_requires_token(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Linear API token must be provided"):
        linear_source(token=None)

def test_source_tags_correctly(monkeypatch):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag
    source = linear_source(token="test-token")
    assert document_source_tag(source) == LINEAR_SOURCE_NAME
