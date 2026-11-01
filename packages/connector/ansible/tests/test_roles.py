"""Tests for Ansible role parsing, task/handler extraction, and dependency chains."""

from __future__ import annotations

from pathlib import Path

from cognee_community_connector_ansible.renderer import render_role_document
from cognee_community_connector_ansible.role import discover_and_parse_roles


def test_discover_and_parse_roles(sample_ansible_project: Path):
    roles = discover_and_parse_roles(sample_ansible_project)
    roles_by_name = {r.name: r for r in roles}

    assert "common" in roles_by_name
    assert "web" in roles_by_name
    assert "app" in roles_by_name

    # Check common role
    common = roles_by_name["common"]
    assert "Install base packages" in common.tasks
    assert "Configure SSH daemon" in common.tasks
    assert "Restart sshd" in common.handlers
    assert common.dependencies == []
    assert "ssh_port" in common.default_vars
    assert "sensitive_common_default" in common.default_vars

    # Check web role (depends on common)
    web = roles_by_name["web"]
    assert "Install nginx package" in web.tasks
    assert "Configure nginx virtual host" in web.tasks
    assert "Ensure nginx service is running" in web.tasks
    assert "Reload nginx" in web.handlers
    assert "Restart nginx" in web.handlers
    assert web.dependencies == ["common"]
    assert "http_port" in web.default_vars
    assert "server_name" in web.default_vars

    # Check app role (depends on web -> dependency chain: app -> web -> common)
    app = roles_by_name["app"]
    assert "Deploy application code" in app.tasks
    assert "Run database migrations" in app.tasks
    assert "Restart application" in app.handlers
    assert app.dependencies == ["web"]
    assert "app_env" in app.default_vars
    assert "app_port" in app.default_vars


def test_rendered_role_markdown_content(sample_ansible_project: Path):
    roles = discover_and_parse_roles(sample_ansible_project)
    web_role = next(r for r in roles if r.name == "web")

    mock_playbook_refs = [
        {"playbook": "site.yml", "play": "Deploy web servers", "targets": "webservers"}
    ]
    doc = render_role_document(web_role, mock_playbook_refs)

    assert doc["id"] == "ansible:role:web"
    assert doc["title"] == "Role: web"

    content = doc["content"]
    # Verify tasks are rendered
    assert "Install nginx package" in content
    assert "Configure nginx virtual host" in content
    # Verify handlers are rendered
    assert "Reload nginx" in content
    # Verify dependencies are explicitly stated
    assert "Role `web` depends on `common`." in content
    # Verify default variable names are listed
    assert "`http_port`" in content
    assert "`server_name`" in content
    # Verify playbook usage is rendered
    assert "Playbook: `site.yml`" in content
    assert "Deploy web servers" in content
