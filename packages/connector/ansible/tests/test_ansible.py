"""Tests for the Ansible Cognee Connector."""

import json
from pathlib import Path

import pytest

from cognee_community_connector_ansible import (
    ANSIBLE_SOURCE_NAME,
    ANSIBLE_TABLE_NAME,
    AnsibleParser,
    ansible_source,
    format_host_to_markdown,
    format_inventory_group_to_markdown,
    format_playbook_to_markdown,
    format_role_to_markdown,
    project_to_cognee_documents,
)
from cognee_community_connector_ansible.cli import main as cli_main


@pytest.fixture
def ansible_repo(tmp_path: Path) -> Path:
    """Create a realistic fixture Ansible repository."""
    # 1. Inventory INI
    inventory_content = """
[webservers]
web1.example.com ansible_host=10.0.1.10 ansible_port=22
web2.example.com ansible_host=10.0.1.11

[databases]
db1.example.com ansible_host=10.0.2.10

[production:children]
webservers
databases

[webservers:vars]
http_port=80
db_password=super_secret_password
"""
    (tmp_path / "hosts.ini").write_text(inventory_content, encoding="utf-8")

    # 2. Roles: common, nginx, postgres
    # Role: common
    common_tasks = """
- name: Install baseline packages
  ansible.builtin.apt:
    name: [curl, git, htop]
    state: present
"""
    (tmp_path / "roles" / "common" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "common" / "tasks" / "main.yml").write_text(
        common_tasks, encoding="utf-8"
    )

    # Role: nginx (depends on common)
    (tmp_path / "roles" / "nginx" / "meta").mkdir(parents=True)
    (tmp_path / "roles" / "nginx" / "meta" / "main.yml").write_text(
        """
dependencies:
  - role: common
""",
        encoding="utf-8",
    )

    (tmp_path / "roles" / "nginx" / "defaults").mkdir(parents=True)
    (tmp_path / "roles" / "nginx" / "defaults" / "main.yml").write_text(
        """
nginx_port: 80
nginx_worker_processes: 4
secret_token: secret_value_here
""",
        encoding="utf-8",
    )

    (tmp_path / "roles" / "nginx" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "nginx" / "tasks" / "main.yml").write_text(
        """
- name: Deploy nginx configuration
  ansible.builtin.template:
    src: nginx.conf.j2
    dest: /etc/nginx/nginx.conf
  notify: Restart nginx
""",
        encoding="utf-8",
    )

    (tmp_path / "roles" / "nginx" / "handlers").mkdir(parents=True)
    (tmp_path / "roles" / "nginx" / "handlers" / "main.yml").write_text(
        """
- name: Restart nginx
  ansible.builtin.service:
    name: nginx
    state: restarted
""",
        encoding="utf-8",
    )

    # 3. Playbooks: site.yml and db.yml
    site_playbook = """
---
- name: Configure Web Application Tier
  hosts: webservers
  roles:
    - common
    - role: nginx
  tasks:
    - name: Ensure app directory exists
      ansible.builtin.file:
        path: /var/www/app
        state: directory
"""
    (tmp_path / "site.yml").write_text(site_playbook, encoding="utf-8")

    # 4. Vault encrypted file (must be skipped / redacted)
    vault_file_content = """$ANSIBLE_VAULT;1.1;AES256
38303862356536336338386438346162393339656461663161356536343534346433616639356336
3230626338343166366539353730303037383236373733350a323363653133333335356133333934
"""
    (tmp_path / "vault_secrets.yml").write_text(vault_file_content, encoding="utf-8")

    return tmp_path


def test_ini_inventory_parsing(ansible_repo: Path) -> None:
    """Verify hosts, groups, and parent-child group relationships in INI."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    assert "webservers" in project.groups
    assert "databases" in project.groups
    assert "production" in project.groups

    web_group = project.groups["webservers"]
    assert "web1.example.com" in web_group.hosts
    assert "web2.example.com" in web_group.hosts
    assert "production" in web_group.parents

    prod_group = project.groups["production"]
    assert "webservers" in prod_group.children
    assert "databases" in prod_group.children

    # Check host connection params
    assert "web1.example.com" in project.hosts
    web1 = project.hosts["web1.example.com"]
    assert web1.ansible_host == "10.0.1.10"
    assert web1.port == 22


def test_yaml_inventory_parsing(tmp_path: Path) -> None:
    """Verify YAML inventory hierarchy."""
    yaml_inv = """
all:
  children:
    datacenter:
      hosts:
        dc-host-01:
          ansible_host: 192.168.100.5
      vars:
        domain: internal.corp
"""
    (tmp_path / "inventory.yaml").write_text(yaml_inv, encoding="utf-8")
    parser = AnsibleParser(project_dir=tmp_path)
    project = parser.parse()

    assert "datacenter" in project.groups
    dc_group = project.groups["datacenter"]
    assert "dc-host-01" in dc_group.hosts
    assert "domain" in dc_group.variable_names
    assert "all" in dc_group.parents


def test_roles_and_dependencies(ansible_repo: Path) -> None:
    """Verify role parsing, dependencies, handlers, and tasks."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    assert "nginx" in project.roles
    assert "common" in project.roles

    nginx = project.roles["nginx"]
    assert "common" in nginx.dependencies
    assert "Restart nginx" in nginx.handlers

    # Check defaults/vars names kept without values
    assert "nginx_port" in nginx.variable_names
    assert "secret_token" in nginx.variable_names

    # Check tasks and touched file
    assert len(nginx.tasks) == 1
    assert nginx.tasks[0].name == "Deploy nginx configuration"
    assert "/etc/nginx/nginx.conf" in nginx.tasks[0].files_touched


def test_playbook_parsing(ansible_repo: Path) -> None:
    """Verify playbook plays, hosts targeting, and roles list."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    assert len(project.playbooks) == 1
    pb = project.playbooks[0]
    assert pb.name == "site.yml"
    assert len(pb.plays) == 1

    play = pb.plays[0]
    assert play.name == "Configure Web Application Tier"
    assert play.hosts_pattern == "webservers"
    assert "common" in play.roles
    assert "nginx" in play.roles
    assert len(play.tasks) == 1
    assert "/var/www/app" in play.tasks[0].files_touched


def test_vault_secrets_never_leak(ansible_repo: Path) -> None:
    """Verify encrypted vault files and sensitive variable values never leak."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    # The vault_secrets.yml file must be detected and skipped
    assert project.vault_files_count == 1

    docs = project_to_cognee_documents(project)
    for doc in docs:
        content = doc["content"]
        # Raw vault header or ciphertext must never be present
        assert "$ANSIBLE_VAULT" not in content
        assert "3830386235653633" not in content
        # Sensitive passwords from inventory must not leak
        assert "super_secret_password" not in content
        assert "secret_value_here" not in content


def test_yaml_inline_vault_tag_redaction(tmp_path: Path) -> None:
    """Verify !vault YAML tags are safely replaced without error."""
    yaml_with_vault = """
- name: Play with secret
  hosts: all
  vars:
    api_token: !vault |
          $ANSIBLE_VAULT;1.1;AES256
          666532653265...
  tasks:
    - name: Ping
      ansible.builtin.ping:
"""
    (tmp_path / "test_vault.yml").write_text(yaml_with_vault, encoding="utf-8")
    parser = AnsibleParser(project_dir=tmp_path)
    project = parser.parse()

    assert len(project.playbooks) == 1
    docs = project_to_cognee_documents(project)
    for d in docs:
        assert "$ANSIBLE_VAULT" not in d["content"]


def test_relational_markdown_formatters(ansible_repo: Path) -> None:
    """Verify Markdown outputs contain explicit statements for Cognee Cognify."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    group_md = format_inventory_group_to_markdown(project.groups["webservers"])
    assert "Group `webservers` is a child of parent group `production`" in group_md
    assert "Host `web1.example.com` belongs to group `webservers`" in group_md

    role_md = format_role_to_markdown(project.roles["nginx"])
    assert "Role `nginx` depends on role `common`" in role_md
    assert "/etc/nginx/nginx.conf" in role_md

    pb_md = format_playbook_to_markdown(project.playbooks[0])
    assert "targets hosts matching `webservers`" in pb_md
    assert "executes role `nginx` on hosts `webservers`" in pb_md

    host_md = format_host_to_markdown(project.hosts["web1.example.com"])
    assert "Target IP / Connection Address" in host_md
    assert "belongs to group `webservers`" in host_md


def test_dlt_source_and_document_contract(ansible_repo: Path) -> None:
    """Verify dlt source creation and document mode metadata."""
    source = ansible_source(path=ansible_repo)
    assert source.cognee_document_source == ANSIBLE_SOURCE_NAME

    # Materialize documents from generator
    resource = source.resources[ANSIBLE_TABLE_NAME]
    docs = list(resource())
    assert len(docs) >= 5

    doc_ids = {d["id"] for d in docs}
    assert "ansible:group:webservers" in doc_ids
    assert "ansible:group:databases" in doc_ids
    assert "ansible:host:web1.example.com" in doc_ids
    assert "ansible:role:nginx" in doc_ids
    assert "ansible:playbook:site.yml" in doc_ids


def test_cli_execution(ansible_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Verify all CLI commands."""
    # 1. version
    assert cli_main(["version"]) == 0
    captured = capsys.readouterr()
    assert "cognee-community-connector-ansible 0.1.0" in captured.out

    # 2. parse
    assert cli_main(["parse", str(ansible_repo)]) == 0
    captured = capsys.readouterr()
    assert "Inventory Groups: 3" in captured.out
    assert "Playbooks: 1" in captured.out
    assert "Roles: 2" in captured.out

    # 3. parse --json
    assert cli_main(["parse", str(ansible_repo), "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["groups_count"] == 3
    assert "nginx" in data["roles"]

    # 4. dump to directory
    out_dir = tmp_path / "dump_output"
    assert cli_main(["dump", str(ansible_repo), "-o", str(out_dir)]) == 0
    assert out_dir.is_dir()
    dumped_files = list(out_dir.glob("*.md"))
    assert len(dumped_files) >= 5

    # 5. audit
    assert cli_main(["audit", str(ansible_repo)]) == 0
    captured = capsys.readouterr()
    assert "Zero vault secrets leaked" in captured.out


def test_question_answering_reasoning(ansible_repo: Path) -> None:
    """Verify graph reasoning: answering 'Which hosts get the nginx role?'."""
    parser = AnsibleParser(project_dir=ansible_repo)
    project = parser.parse()

    # 1. Find all plays that execute role 'nginx'
    target_hosts_patterns: list[str] = []
    for pb in project.playbooks:
        for play in pb.plays:
            if "nginx" in play.roles:
                target_hosts_patterns.append(play.hosts_pattern)

    assert target_hosts_patterns == ["webservers"]

    # 2. Resolve target group to hosts
    nginx_hosts: list[str] = []
    for pattern in target_hosts_patterns:
        if pattern in project.groups:
            nginx_hosts.extend(project.groups[pattern].hosts)

    assert sorted(nginx_hosts) == ["web1.example.com", "web2.example.com"]
