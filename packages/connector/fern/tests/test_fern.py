"""Unit tests for the Fern API documentation dlt connector.

Covers:
- fern.config.json parsing and directory resolution
- Fern YAML endpoint definition rendering (parameters, request/response, errors)
- Markdown guide parsing and frontmatter extraction
- Full-snapshot sync and forget-on-delete semantics
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cognee_community_connector_fern.fern import (
    FERN_SOURCE_NAME,
    _load_fern_config,
    _render_endpoint,
    _render_guide,
    _resolve_fern_dirs,
    fern_source,
)


@pytest.fixture
def mock_fern_workspace(tmp_path: Path) -> Path:
    """Creates a temporary mock Fern definition workspace."""
    fern_dir = tmp_path / "fern"
    fern_dir.mkdir(parents=True, exist_ok=True)

    # 1. fern.config.json
    config = {"organization": "acme-ai", "version": "0.15.0"}
    (fern_dir / "fern.config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # 2. definition/ users.yml
    def_dir = fern_dir / "definition"
    def_dir.mkdir(parents=True, exist_ok=True)

    users_api = {
        "service": {
            "base-path": "/users",
            "auth": True,
            "endpoints": {
                "getUser": {
                    "method": "GET",
                    "path": "/{userId}",
                    "display-name": "Get User Details",
                    "docs": "Retrieve comprehensive user profile information.",
                    "path-parameters": {
                        "userId": {"type": "string", "docs": "Target user identifier"}
                    },
                    "request": {
                        "query-parameters": {
                            "include_metadata": {
                                "type": "boolean",
                                "docs": "Include extended profile",
                            }
                        }
                    },
                    "response": {"type": "User"},
                    "errors": ["UserNotFoundError"],
                }
            },
        }
    }
    (def_dir / "users.yml").write_text(yaml.dump(users_api), encoding="utf-8")

    # 3. pages/overview.md
    pages_dir = fern_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    overview_text = """---
title: Fern API Overview
slug: /overview
---

# Overview

Welcome to the Acme AI API platform reference.
"""
    (pages_dir / "overview.md").write_text(overview_text, encoding="utf-8")

    return tmp_path


def test_load_fern_config(mock_fern_workspace: Path):
    cfg = _load_fern_config(mock_fern_workspace)
    assert cfg.get("organization") == "acme-ai"

    empty = _load_fern_config(mock_fern_workspace / "nonexistent")
    assert empty == {}


def test_resolve_fern_dirs(mock_fern_workspace: Path):
    fern_root, def_dir = _resolve_fern_dirs(mock_fern_workspace)
    assert fern_root.name == "fern"
    assert def_dir.name == "definition"


def test_render_endpoint():
    ep_data = {
        "method": "POST",
        "path": "/v1/chat/completions",
        "display-name": "Create Completion",
        "docs": "Generates a model completion.",
        "request": {
            "query-parameters": {"stream": "boolean"},
            "body": {"type": "CompletionRequest"},
        },
        "response": {"type": "CompletionResponse"},
        "errors": ["RateLimitError"],
    }
    row = _render_endpoint(
        service_name="chat",
        base_path="",
        endpoint_id="createCompletion",
        endpoint_data=ep_data,
    )

    assert row["id"] == "fern:endpoint:chat:createCompletion"
    assert row["url"] == "/v1/chat/completions"
    assert "POST /v1/chat/completions - Create Completion" in row["title"]
    assert "**Endpoint:** `POST /v1/chat/completions`" in row["content"]
    assert "- **stream** (`boolean`)" in row["content"]
    assert "Type: `CompletionRequest`" in row["content"]
    assert "Returns: `CompletionResponse`" in row["content"]
    assert "- `RateLimitError`" in row["content"]


def test_render_guide(mock_fern_workspace: Path):
    guide_path = mock_fern_workspace / "fern" / "pages" / "overview.md"
    row = _render_guide(guide_path, mock_fern_workspace / "fern")

    assert row["id"] == "fern:guide:pages/overview"
    assert row["title"] == "Fern API Overview"
    assert "Welcome to the Acme AI API platform" in row["content"]


def test_fern_source_raises_on_invalid_dir(tmp_path: Path):
    with pytest.raises(ValueError, match="Valid Fern directory required"):
        fern_source(fern_path=tmp_path / "does_not_exist")


def test_full_snapshot_sync_and_forget_on_delete(mock_fern_workspace: Path):
    # Run 1: 1 endpoint + 1 guide = 2 docs
    source = fern_source(fern_path=mock_fern_workspace)
    assert source.name == FERN_SOURCE_NAME

    resource = next(iter(source.resources.values()))
    assert resource.write_disposition == "replace"

    rows_1 = list(resource())
    assert len(rows_1) == 2
    ids_1 = {r["id"] for r in rows_1}
    assert "fern:endpoint:users:getUser" in ids_1
    assert "fern:guide:pages/overview" in ids_1

    # Simulate deleting the guide upstream
    (mock_fern_workspace / "fern" / "pages" / "overview.md").unlink()

    # Run 2: Full-snapshot replace drops the deleted guide (forget-on-delete)
    source_2 = fern_source(fern_path=mock_fern_workspace)
    resource_2 = next(iter(source_2.resources.values()))
    rows_2 = list(resource_2())
    assert len(rows_2) == 1
    assert rows_2[0]["id"] == "fern:endpoint:users:getUser"
