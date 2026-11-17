"""Feishu / Lark Docs & Wiki data-source connector for cognee."""

from .feishu import (
    DOCUMENT_SOURCE_ATTR,
    convert_docx_blocks_to_markdown,
    feishu_source,
    format_feishu_document,
    parse_feishu_workspace,
)

__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "convert_docx_blocks_to_markdown",
    "format_feishu_document",
    "parse_feishu_workspace",
    "feishu_source",
]
