"""Unit tests for the Docusaurus documentation dlt connector.

Covers:
- sidebars.json configuration parsing and category tree traversal
- Admonition converting (:::note, :::tip, :::danger) into clean Markdown
- Frontmatter extraction and document formatting
- Full-snapshot sync and forget-on-delete semantics
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognee_community_connector_docusaurus.docusaurus import (
    DOCUSAURUS_SOURCE_NAME,
    _clean_admonitions,
    _extract_sidebar_items,
    _find_doc_file,
    _load_sidebar_config,
    _page_to_row,
    _parse_frontmatter_and_content,
    docusaurus_source,
)


@pytest.fixture
def mock_docusaurus_site(tmp_path: Path) -> Path:
    """Creates a temporary mock Docusaurus documentation directory."""
    # 1. sidebars.json
    sidebars = {
        "docsSidebar": [
            "intro",
            {
                "type": "category",
                "label": "Guides",
                "items": ["guides/getting-started"],
            },
        ]
    }
    (tmp_path / "sidebars.json").write_text(json.dumps(sidebars, indent=2), encoding="utf-8")

    # 2. docs/ folder
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    # intro.md
    intro_content = """---
title: Introduction to React Platform
sidebar_label: Overview
description: Learn the core concepts of our framework
---

# Introduction

:::note
This is an introductory guide for developers.
:::

:::tip Performance Tip
Always memoize expensive computations.
:::
"""
    (docs_dir / "intro.md").write_text(intro_content, encoding="utf-8")

    # guides/getting-started.mdx
    guides_dir = docs_dir / "guides"
    guides_dir.mkdir(parents=True, exist_ok=True)

    getting_started = """---
title: Getting Started
description: Fast step-by-step setup
slug: /custom-getting-started
---

:::danger Breaking Change
Legacy v1 configurations are no longer supported.
:::

## Installation

```bash
npm install @example/core
```
"""
    (guides_dir / "getting-started.mdx").write_text(getting_started, encoding="utf-8")

    return tmp_path


def test_load_sidebar_config(mock_docusaurus_site: Path):
    cfg = _load_sidebar_config(mock_docusaurus_site)
    assert "docsSidebar" in cfg

    empty = _load_sidebar_config(mock_docusaurus_site / "nonexistent")
    assert empty == {}


def test_extract_sidebar_items():
    items = [
        "intro",
        {
            "type": "category",
            "label": "Basics",
            "items": [
                {"type": "doc", "id": "basics/setup"},
                "basics/config",
            ],
        },
    ]
    extracted = _extract_sidebar_items(items, breadcrumbs=["Main"])
    assert len(extracted) == 3
    assert extracted[0] == ("intro", ["Main"])
    assert extracted[1] == ("basics/setup", ["Main", "Basics"])
    assert extracted[2] == ("basics/config", ["Main", "Basics"])


def test_find_doc_file(mock_docusaurus_site: Path):
    docs = mock_docusaurus_site / "docs"
    f1 = _find_doc_file(docs, "intro")
    assert f1 is not None and f1.name == "intro.md"

    f2 = _find_doc_file(docs, "guides/getting-started")
    assert f2 is not None and f2.name == "getting-started.mdx"

    f3 = _find_doc_file(docs, "nonexistent")
    assert f3 is None


def test_parse_frontmatter_and_content(mock_docusaurus_site: Path):
    f = mock_docusaurus_site / "docs" / "intro.md"
    fm, body = _parse_frontmatter_and_content(f)
    assert fm.get("title") == "Introduction to React Platform"
    assert fm.get("sidebar_label") == "Overview"
    assert ":::note" in body


def test_clean_admonitions():
    raw = """
import React from 'react';

:::note
Basic note body.
:::

:::tip Custom Title Here
Pro tip for developers.
:::

:::danger
Critical alert!
:::
"""
    cleaned = _clean_admonitions(raw)
    assert "import React" not in cleaned
    assert "> **Note:**\n> Basic note body." in cleaned
    assert "> **Custom Title Here:**\n> Pro tip for developers." in cleaned
    assert "> **Danger:**\n> Critical alert!" in cleaned


def test_page_to_row(mock_docusaurus_site: Path):
    f = mock_docusaurus_site / "docs" / "guides" / "getting-started.mdx"
    row = _page_to_row(
        doc_id="guides/getting-started",
        breadcrumbs=["docsSidebar", "Guides"],
        file_path=f,
    )
    assert row["id"] == "docusaurus:guides/getting-started"
    assert row["url"] == "/custom-getting-started"
    assert row["title"] == "Getting Started"
    assert "docsSidebar > Guides" in row["content"]
    assert "> **Breaking Change:**" in row["content"]


def test_docusaurus_source_raises_on_invalid_path(tmp_path: Path):
    with pytest.raises(ValueError, match="Valid Docusaurus site directory required"):
        docusaurus_source(site_path=tmp_path / "does_not_exist")


def test_full_snapshot_sync_and_forget_on_delete(mock_docusaurus_site: Path):
    # Run 1: 2 docs present
    source = docusaurus_source(site_path=mock_docusaurus_site)
    assert source.name == DOCUSAURUS_SOURCE_NAME

    resource = next(iter(source.resources.values()))
    assert resource.write_disposition == "replace"

    rows_1 = list(resource())
    assert len(rows_1) == 2
    ids_1 = {r["id"] for r in rows_1}
    assert ids_1 == {
        "docusaurus:intro",
        "docusaurus:guides/getting-started",
    }

    # Simulate deleting a page upstream
    (mock_docusaurus_site / "docs" / "intro.md").unlink()

    # Run 2: Full-snapshot replace drops the deleted page (forget-on-delete)
    source_2 = docusaurus_source(site_path=mock_docusaurus_site)
    resource_2 = next(iter(source_2.resources.values()))
    rows_2 = list(resource_2())
    assert len(rows_2) == 1
    assert rows_2[0]["id"] == "docusaurus:guides/getting-started"


def test_include_docs_filter(mock_docusaurus_site: Path):
    source = docusaurus_source(
        site_path=mock_docusaurus_site,
        include_docs=["intro"],
    )
    resource = next(iter(source.resources.values()))
    rows = list(resource())
    assert len(rows) == 1
    assert rows[0]["id"] == "docusaurus:intro"
