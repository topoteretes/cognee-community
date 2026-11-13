"""Tests for Ansible playbook parsing and relationship extraction."""

from __future__ import annotations

from pathlib import Path

from cognee_community_connector_ansible.playbook import (
    discover_and_parse_playbooks,
    parse_playbook_file,
)
from cognee_community_connector_ansible.renderer import render_playbook_document


def test_discover_and_parse_playbooks(sample_ansible_project: Path):
    playbooks = discover_and_parse_playbooks(sample_ansible_project)
    file_paths = {pb.file_path for pb in playbooks}

    # Should find site.yml at root and playbooks/deploy_app.yml in subdirectory
    assert "site.yml" in file_paths
    assert "playbooks/deploy_app.yml" in file_paths

    site_pb = next(pb for pb in playbooks if pb.file_path == "site.yml")
    assert len(site_pb.plays) == 2

    # Play 1: common setup
    play1 = site_pb.plays[0]
    assert play1.name == "Apply common setup to all servers"
    assert play1.hosts == "all"
    assert play1.roles == ["common"]
    assert "Verify basic connectivity" in play1.tasks

    # Play 2: web servers
    play2 = site_pb.plays[1]
    assert play2.name == "Deploy web servers"
    assert play2.hosts == "webservers"
    assert play2.roles == ["web"]
    assert "Check web health status" in play2.tasks

    # Playbook 2: deploy_app.yml
    app_pb = next(pb for pb in playbooks if pb.file_path == "playbooks/deploy_app.yml")
    assert len(app_pb.plays) == 1
    app_play = app_pb.plays[0]
    assert app_play.name == "Deploy application stack"
    assert app_play.hosts == "appservers"
    assert app_play.roles == ["app"]
    assert "Warm up application cache" in app_play.tasks


def test_playbook_with_dynamic_role_inclusion(tmp_path: Path):
    pb_file = tmp_path / "dynamic_roles.yml"
    pb_file.write_text(
        """
- name: Play with dynamic role includes
  hosts: dbservers
  tasks:
    - name: Dynamically include database role
      include_role:
        name: postgresql
    - name: Import another role
      import_role:
        name: redis
""".strip(),
        encoding="utf-8",
    )

    pb = parse_playbook_file(pb_file, tmp_path)
    assert pb is not None
    assert len(pb.plays) == 1
    play = pb.plays[0]
    assert "postgresql" in play.roles
    assert "redis" in play.roles


def test_rendered_playbook_markdown_content(sample_ansible_project: Path):
    playbooks = discover_and_parse_playbooks(sample_ansible_project)
    site_pb = next(pb for pb in playbooks if pb.file_path == "site.yml")

    doc = render_playbook_document(site_pb)
    assert doc["id"] == "ansible:playbook:site.yml"
    assert doc["title"] == "Playbook: site.yml"

    content = doc["content"]
    # Check that play names and target groups are explicitly stated
    assert "### Play: Deploy web servers" in content
    assert "**Targets:** `webservers`" in content
    assert "**Roles:** `web`" in content
    assert (
        "Play `Deploy web servers` targets group/hosts `webservers` and applies roles: `web`."
        in content
    )
