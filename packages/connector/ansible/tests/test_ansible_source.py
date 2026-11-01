"""Tests for ansible_source DLT document source integration."""

from __future__ import annotations

from pathlib import Path

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, document_source_tag

from cognee_community_connector_ansible import (
    ANSIBLE_SOURCE_NAME,
    ANSIBLE_TABLE_NAME,
    ansible_source,
)


def test_ansible_source_returns_tagged_dlt_source(sample_ansible_project: Path):
    source = ansible_source(sample_ansible_project)

    # Document-mode tag verification
    assert document_source_tag(source) == ANSIBLE_SOURCE_NAME
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == ANSIBLE_SOURCE_NAME

    # Resource properties
    resource = source.resources[ANSIBLE_TABLE_NAME]
    assert resource.name == ANSIBLE_TABLE_NAME
    assert resource.write_disposition == "replace"
    hints = getattr(resource, "_hints", {}) or getattr(resource, "hints", {})
    assert hints.get("primary_key") == "id"


def test_ansible_source_invalid_path_raises():
    with pytest.raises(ValueError, match="Ansible project path does not exist"):
        ansible_source("/path/that/definitely/does/not/exist_123456")


def test_ansible_source_yields_expected_rows(sample_ansible_project: Path):
    source = ansible_source(sample_ansible_project)
    resource = source.resources[ANSIBLE_TABLE_NAME]

    rows = list(resource)
    assert len(rows) > 0

    ids = {r["id"] for r in rows}
    # Check that inventory groups, playbooks, and roles are present
    assert "ansible:group:webservers" in ids
    assert "ansible:group:production" in ids
    assert "ansible:playbook:site.yml" in ids
    assert "ansible:playbook:playbooks/deploy_app.yml" in ids
    assert "ansible:role:common" in ids
    assert "ansible:role:web" in ids
    assert "ansible:role:app" in ids

    # Check row schema
    for r in rows:
        assert "id" in r
        assert "title" in r
        assert "content" in r
        assert "url" in r
