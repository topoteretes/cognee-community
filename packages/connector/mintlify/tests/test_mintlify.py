"""Unit tests for the Mintlify documentation dlt connector.

Covers:
- mint.json configuration parsing and navigation tree traversal
- MDX tag cleaning (Card, ParamField, ResponseField, Accordion, Callouts)
- Frontmatter extraction and page-to-row formatting
- Full-snapshot sync and forget-on-delete semantics
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognee_community_connector_mintlify.mintlify import (
    MINTLIFY_SOURCE_NAME,
    _clean_mdx,
    _extract_navigation_pages,
    _find_page_file,
    _load_mint_config,
    _page_to_row,
    _parse_frontmatter_and_content,
    mintlify_source,
)


@pytest.fixture
def mock_mintlify_workspace(tmp_path: Path) -> Path:
    """Creates a temporary mock Mintlify documentation repository."""
    # 1. mint.json
    config = {
        "name": "Acme API Docs",
        "navigation": [
            {
                "group": "Getting Started",
                "pages": ["introduction", "quickstart"],
            },
            {
                "group": "API Reference",
                "pages": [
                    {
                        "group": "Authentication",
                        "pages": ["api/auth"],
                    }
                ],
            },
        ],
    }
    (tmp_path / "mint.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # 2. introduction.mdx
    intro_content = """---
title: Introduction to Acme
description: Overview of the Acme platform and architecture
---

import { CustomComponent } from '@/components';

# Welcome to Acme

<Tip>
Ensure your environment is configured before starting.
</Tip>

<Card title="Quickstart Guide" icon="rocket">
Get up and running in under five minutes.
</Card>
"""
    (tmp_path / "introduction.mdx").write_text(intro_content, encoding="utf-8")

    # 3. quickstart.md
    quickstart_content = """---
title: Quickstart
description: 5-minute fast setup
---

## Step 1: Install SDK

```bash
pip install acme-sdk
```
"""
    (tmp_path / "quickstart.md").write_text(quickstart_content, encoding="utf-8")

    # 4. api/auth.mdx (nested folder)
    api_dir = tmp_path / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    auth_content = """---
title: Authenticate Requests
api: POST /v1/auth/token
---

<ParamField path="client_id" type="string" required>
The unique client identifier.
</ParamField>

<ResponseField name="token" type="string">
The generated JWT bearer token.
</ResponseField>
"""
    (api_dir / "auth.mdx").write_text(auth_content, encoding="utf-8")

    return tmp_path


def test_load_mint_config(mock_mintlify_workspace: Path):
    config = _load_mint_config(mock_mintlify_workspace)
    assert config.get("name") == "Acme API Docs"
    assert "navigation" in config

    empty = _load_mint_config(mock_mintlify_workspace / "nonexistent")
    assert empty == {}


def test_extract_navigation_pages():
    nav = [
        {"group": "Guide", "pages": ["guide/intro", "guide/usage"]},
        {
            "group": "API",
            "pages": [
                {"group": "Endpoints", "pages": ["api/users"]},
            ],
        },
    ]
    extracted = _extract_navigation_pages(nav)
    assert len(extracted) == 3
    assert extracted[0] == ("guide/intro", ["Guide"])
    assert extracted[1] == ("guide/usage", ["Guide"])
    assert extracted[2] == ("api/users", ["API", "Endpoints"])


def test_find_page_file(mock_mintlify_workspace: Path):
    # Finds .mdx
    p1 = _find_page_file(mock_mintlify_workspace, "introduction")
    assert p1 is not None and p1.name == "introduction.mdx"

    # Finds .md
    p2 = _find_page_file(mock_mintlify_workspace, "quickstart")
    assert p2 is not None and p2.name == "quickstart.md"

    # Finds nested
    p3 = _find_page_file(mock_mintlify_workspace, "api/auth")
    assert p3 is not None and p3.name == "auth.mdx"

    # Returns None for missing
    p4 = _find_page_file(mock_mintlify_workspace, "missing-page")
    assert p4 is None


def test_parse_frontmatter_and_content(mock_mintlify_workspace: Path):
    file_path = mock_mintlify_workspace / "introduction.mdx"
    fm, body = _parse_frontmatter_and_content(file_path)

    assert fm.get("title") == "Introduction to Acme"
    assert fm.get("description") == "Overview of the Acme platform and architecture"
    assert "# Welcome to Acme" in body


def test_clean_mdx():
    raw_mdx = """
import { Test } from '@/test';

<Note>
Take note of this important rule.
</Note>

<Card title="Features" icon="star">
List of awesome features.
</Card>

<Accordion title="Click to expand">
Hidden secret details.
</Accordion>

<ParamField path="api_key" type="string">
Secret token.
</ParamField>

<ResponseField name="status" type="integer">
HTTP status code.
</ResponseField>
"""
    cleaned = _clean_mdx(raw_mdx)

    assert "import { Test }" not in cleaned
    assert "> **Note:**" in cleaned
    assert "Take note of this important rule." in cleaned
    assert "### Features" in cleaned
    assert "List of awesome features." in cleaned
    assert "**Click to expand**" in cleaned
    assert "- **api_key** (`string`): Secret token." in cleaned
    assert "- **status** (`integer`): HTTP status code." in cleaned


def test_page_to_row(mock_mintlify_workspace: Path):
    file_path = mock_mintlify_workspace / "api" / "auth.mdx"
    row = _page_to_row(
        page_slug="api/auth",
        breadcrumbs=["API Reference", "Authentication"],
        file_path=file_path,
        site_title="Acme API Docs",
    )

    assert row["id"] == "mintlify:api/auth"
    assert row["url"] == "/api/auth"
    assert row["title"] == "Authenticate Requests"
    assert "# Acme API Docs" in row["content"]
    assert "API Reference > Authentication" in row["content"]
    assert "`API: POST /v1/auth/token`" in row["content"]
    assert "- **client_id** (`string`):" in row["content"]


def test_mintlify_source_raises_on_invalid_dir(tmp_path: Path):
    invalid = tmp_path / "does_not_exist"
    with pytest.raises(ValueError, match="Valid Mintlify docs directory required"):
        mintlify_source(docs_path=invalid)


def test_mintlify_source_full_snapshot_and_forget_on_delete(mock_mintlify_workspace: Path):
    # Run 1: All 3 pages present
    source = mintlify_source(docs_path=mock_mintlify_workspace)
    assert source.name == MINTLIFY_SOURCE_NAME

    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.write_disposition == "replace"

    rows_run_1 = list(resource())
    assert len(rows_run_1) == 3
    ids_run_1 = {r["id"] for r in rows_run_1}
    assert ids_run_1 == {
        "mintlify:introduction",
        "mintlify:quickstart",
        "mintlify:api/auth",
    }

    # Simulate deleting a page upstream from the docs repository
    (mock_mintlify_workspace / "quickstart.md").unlink()

    # Run 2: Full-snapshot replace drops the deleted page automatically (forget-on-delete)
    source_run_2 = mintlify_source(docs_path=mock_mintlify_workspace)
    resource_2 = next(iter(source_run_2.resources.values()))
    rows_run_2 = list(resource_2())
    assert len(rows_run_2) == 2
    ids_run_2 = {r["id"] for r in rows_run_2}
    assert "mintlify:quickstart" not in ids_run_2
    assert ids_run_2 == {
        "mintlify:introduction",
        "mintlify:api/auth",
    }


def test_mintlify_source_include_pages(mock_mintlify_workspace: Path):
    source = mintlify_source(
        docs_path=mock_mintlify_workspace,
        include_pages=["introduction"],
    )
    resource = next(iter(source.resources.values()))
    rows = list(resource())
    assert len(rows) == 1
    assert rows[0]["id"] == "mintlify:introduction"
