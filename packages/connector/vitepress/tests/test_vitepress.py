"""Unit tests for the VitePress documentation dlt connector.

Covers:
- .vitepress/config.json sidebar parsing and category traversal
- Custom container converting (::: tip, ::: warning, ::: danger) into clean Markdown
- Frontmatter extraction and document formatting
- Full-snapshot sync and forget-on-delete semantics
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognee_community_connector_vitepress.vitepress import (
    VITEPRESS_SOURCE_NAME,
    _clean_containers,
    _extract_sidebar_items,
    _find_doc_file,
    _load_vitepress_config,
    _page_to_row,
    _parse_frontmatter_and_content,
    vitepress_source,
)


@pytest.fixture
def mock_vitepress_site(tmp_path: Path) -> Path:
    """Creates a temporary mock VitePress documentation workspace."""
    # 1. .vitepress/config.json
    vp_dir = tmp_path / ".vitepress"
    vp_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "title": "VitePress Docs",
        "themeConfig": {
            "sidebar": [
                {
                    "text": "Guide",
                    "items": [
                        {"text": "Getting Started", "link": "/guide/getting-started"},
                    ],
                }
            ]
        },
    }
    (vp_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # 2. guide/getting-started.md
    guide_dir = tmp_path / "guide"
    guide_dir.mkdir(parents=True, exist_ok=True)
    getting_started = """---
title: Getting Started with VitePress
description: Fast setup guide
---

# Getting Started

::: tip Performance
VitePress produces ultra-fast static bundles.
:::

::: warning Prerequisite
Node.js 18+ is required.
:::
"""
    (guide_dir / "getting-started.md").write_text(getting_started, encoding="utf-8")

    # 3. index.md
    (tmp_path / "index.md").write_text("# Home\n\nWelcome to VitePress.", encoding="utf-8")

    return tmp_path


def test_load_vitepress_config(mock_vitepress_site: Path):
    cfg = _load_vitepress_config(mock_vitepress_site)
    assert cfg.get("title") == "VitePress Docs"

    empty = _load_vitepress_config(mock_vitepress_site / "nonexistent")
    assert empty == {}


def test_extract_sidebar_items():
    sidebar = [
        {
            "text": "Section",
            "items": [
                {"text": "Intro", "link": "/intro"},
                {"text": "Config", "link": "/config"},
            ],
        }
    ]
    items = _extract_sidebar_items(sidebar)
    assert len(items) == 2
    assert items[0] == ("/intro", ["Section"])
    assert items[1] == ("/config", ["Section"])


def test_find_doc_file(mock_vitepress_site: Path):
    f1 = _find_doc_file(mock_vitepress_site, "/guide/getting-started")
    assert f1 is not None and f1.name == "getting-started.md"

    f2 = _find_doc_file(mock_vitepress_site, "index")
    assert f2 is not None and f2.name == "index.md"

    f3 = _find_doc_file(mock_vitepress_site, "missing")
    assert f3 is None


def test_parse_frontmatter_and_content(mock_vitepress_site: Path):
    f = mock_vitepress_site / "guide" / "getting-started.md"
    fm, body = _parse_frontmatter_and_content(f)
    assert fm.get("title") == "Getting Started with VitePress"
    assert "::: tip" in body


def test_clean_containers():
    raw = """
<script setup>
import { ref } from 'vue';
</script>

::: tip Pro Tip
Always use composition API.
:::

::: warning Notice
Do not mutate state directly.
:::
"""
    cleaned = _clean_containers(raw)
    assert "<script" not in cleaned
    assert "> **Pro Tip:**\n> Always use composition API." in cleaned
    assert "> **Notice:**\n> Do not mutate state directly." in cleaned


def test_page_to_row(mock_vitepress_site: Path):
    f = mock_vitepress_site / "guide" / "getting-started.md"
    row = _page_to_row(
        link="/guide/getting-started",
        breadcrumbs=["Guide"],
        file_path=f,
    )
    assert row["id"] == "vitepress:guide/getting-started"
    assert row["url"] == "/guide/getting-started"
    assert row["title"] == "Getting Started with VitePress"
    assert "Guide" in row["content"]
    assert "> **Pro Tip:**" not in row["content"]
    assert "> **Performance:**" in row["content"]


def test_vitepress_source_raises_on_invalid_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="Valid VitePress directory required"):
        vitepress_source(site_path=tmp_path / "does_not_exist")


def test_full_snapshot_sync_and_forget_on_delete(mock_vitepress_site: Path):
    # Run 1: 2 docs present (getting-started and index)
    source = vitepress_source(site_path=mock_vitepress_site)
    assert source.name == VITEPRESS_SOURCE_NAME

    resource = next(iter(source.resources.values()))
    assert resource.write_disposition == "replace"

    rows_1 = list(resource())
    assert len(rows_1) == 2
    ids_1 = {r["id"] for r in rows_1}
    assert "vitepress:guide/getting-started" in ids_1
    assert "vitepress:index" in ids_1

    # Simulate deleting the guide upstream
    (mock_vitepress_site / "guide" / "getting-started.md").unlink()

    # Run 2: Full-snapshot replace drops the deleted guide (forget-on-delete)
    source_2 = vitepress_source(site_path=mock_vitepress_site)
    resource_2 = next(iter(source_2.resources.values()))
    rows_2 = list(resource_2())
    assert len(rows_2) == 1
    assert rows_2[0]["id"] == "vitepress:index"
