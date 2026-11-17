"""Tests for Feishu / Lark Docs & Wiki connector for cognee."""

import pytest
from pathlib import Path
from cognee_community_connector_feishu.feishu import (
    DOCUMENT_SOURCE_ATTR,
    convert_docx_blocks_to_markdown,
    format_feishu_document,
    parse_feishu_workspace,
    feishu_source,
)


@pytest.fixture
def mock_feishu_doc_blocks():
    return [
        {
            "block_type": 3,  # H1
            "heading1": {"elements": [{"text_run": {"content": "Base Mainnet Autonomous Agent Spec"}}]},
        },
        {
            "block_type": 2,  # Text
            "text": {
                "elements": [
                    {"text_run": {"content": "This document outlines the "}},
                    {"text_run": {"content": "x402 Micropayment Protocol", "text_element_style": {"bold": True}}},
                    {"text_run": {"content": " integration with "}},
                    {"text_run": {"content": "ERC-4337", "text_element_style": {"inline_code": True}}},
                    {"text_run": {"content": " smart accounts."}},
                ]
            },
        },
        {
            "block_type": 11,  # Bullet
            "bullet": {"elements": [{"text_run": {"content": "Zero upfront capital requirement."}}]},
        },
        {
            "block_type": 11,  # Bullet
            "bullet": {"elements": [{"text_run": {"content": "Automated 24/7 background sentinel daemon."}}]},
        },
        {
            "block_type": 14,  # Code block
            "code": {
                "style": {"language": 1},
                "elements": [{"text_run": {"content": "def settle_payment():\n    return '0xSUCCESS'"}}],
            },
        },
        {
            "block_type": 18,  # Callout
            "callout": {"elements": [{"text_run": {"content": "Security review verified by David & maintainers."}}]},
        },
    ]


@pytest.fixture
def mock_feishu_workspace(mock_feishu_doc_blocks):
    return {
        "documents": [
            {
                "document_id": "docx_88992211",
                "title": "Base Mainnet Autonomous Agent Spec",
                "space_id": "space_rnd_asiapac",
                "space_name": "R&D Protocols Wiki",
                "author": "Rizul Chauhan",
                "updated_at": "2026-10-24 16:30:00",
                "blocks": mock_feishu_doc_blocks,
            }
        ]
    }


def test_convert_docx_blocks_to_markdown(mock_feishu_doc_blocks):
    md = convert_docx_blocks_to_markdown(mock_feishu_doc_blocks)
    assert "# Base Mainnet Autonomous Agent Spec" in md
    assert "**x402 Micropayment Protocol**" in md
    assert "`ERC-4337`" in md
    assert "- Zero upfront capital requirement." in md
    assert "- Automated 24/7 background sentinel daemon." in md
    assert "```python\ndef settle_payment():" in md
    assert "> [!NOTE]" in md
    assert "Security review verified by David" in md


def test_format_feishu_document(mock_feishu_workspace):
    doc = mock_feishu_workspace["documents"][0]
    md = format_feishu_document(doc, domain="feishu")
    assert "[Feishu Document] Base Mainnet Autonomous Agent Spec" in md
    assert "Document ID:** `docx_88992211`" in md
    assert "Wiki Space:** `R&D Protocols Wiki`" in md
    assert "Author / Owner:** `Rizul Chauhan`" in md
    assert "**x402 Micropayment Protocol**" in md


def test_parse_feishu_workspace(mock_feishu_workspace):
    docs = parse_feishu_workspace(mock_feishu_workspace, domain="feishu")
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "feishu_feishu_docx_88992211"
    assert "feishu:doc:docx_88992211" in doc["node_sets"]
    assert "feishu:space:space_rnd_asiapac" in doc["node_sets"]
    assert doc["metadata"]["source"] == DOCUMENT_SOURCE_ATTR
    assert doc["metadata"]["title"] == "Base Mainnet Autonomous Agent Spec"


def test_missing_snapshot_file_error():
    with pytest.raises(FileNotFoundError):
        feishu_source(snapshot_data="/non/existent/feishu_snapshot.json")


def test_feishu_source_with_snapshot(mock_feishu_workspace):
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt is not installed in the test environment")

    source = feishu_source(snapshot_data=mock_feishu_workspace)
    assert source is not None
