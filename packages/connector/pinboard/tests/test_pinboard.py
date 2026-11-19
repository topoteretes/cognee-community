import pytest
from unittest.mock import MagicMock
from cognee_community_connector_pinboard.pinboard import (
    pinboard_source,
    fetch_pinboard_posts,
    format_pinboard_post_markdown
)

MOCK_POSTS = [
    {
        "href": "https://docs.cognee.ai",
        "description": "Cognee AI Memory Documentation",
        "extended": "Deep guide on cognify, graph structures, and semantic search.",
        "tags": "ai python knowledge-graph",
        "time": "2026-10-26T04:00:00Z",
        "shared": "yes",
        "toread": "no"
    },
    {
        "href": "https://nebius.com",
        "description": "Nebius Token Factory",
        "extended": "Serverless inference for open-source AI models.",
        "tags": "cloud inference nvidia",
        "time": "2026-10-26T04:30:00Z",
        "shared": "yes",
        "toread": "yes"
    }
]

def test_missing_token_raises():
    with pytest.raises(ValueError, match="Pinboard auth_token must be provided"):
        pinboard_source(auth_token="")

def test_format_post_markdown():
    post = MOCK_POSTS[0]
    md = format_pinboard_post_markdown(post)
    assert "# Cognee AI Memory Documentation" in md
    assert "[https://docs.cognee.ai](https://docs.cognee.ai)" in md
    assert "`ai python knowledge-graph`" in md
    assert "Deep guide on cognify" in md
    assert "- **To Read Later**: No" in md

def test_fetch_pinboard_mocked():
    session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MOCK_POSTS
    session.get.return_value = mock_resp

    posts = fetch_pinboard_posts(auth_token="user:SECRET_TOKEN", session=session)
    assert len(posts) == 2
    assert posts[0]["description"] == "Cognee AI Memory Documentation"

def test_pinboard_source_documents():
    session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MOCK_POSTS
    session.get.return_value = mock_resp

    docs = pinboard_source(auth_token="user:SECRET_TOKEN", session=session)
    assert len(docs) == 2
    doc1 = docs[0]
    assert doc1["title"] == "Pinboard: Cognee AI Memory Documentation"
    assert doc1["metadata"]["source"] == "pinboard"
    assert doc1["metadata"]["url"] == "https://docs.cognee.ai"
    assert "ai" in doc1["metadata"]["tags"]
    assert len(doc1["id"]) == 64  # SHA-256 hash length
