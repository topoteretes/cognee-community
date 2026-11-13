"""Tests for Ansible inventory parsing (INI and YAML) and group relationships."""

from __future__ import annotations

from pathlib import Path

from cognee_community_connector_ansible.inventory import (
    InventoryData,
    discover_and_parse_inventory,
    expand_host_pattern,
    parse_yaml_inventory,
)
from cognee_community_connector_ansible.renderer import render_inventory_group_document


def test_expand_host_pattern_numeric():
    assert expand_host_pattern("web[1:3].example.com") == [
        "web1.example.com",
        "web2.example.com",
        "web3.example.com",
    ]
    assert expand_host_pattern("node[01:03].internal") == [
        "node01.internal",
        "node02.internal",
        "node03.internal",
    ]


def test_expand_host_pattern_alphabetic():
    assert expand_host_pattern("db-[a:c].prod") == [
        "db-a.prod",
        "db-b.prod",
        "db-c.prod",
    ]


def test_ini_inventory_parsing(sample_ansible_project: Path):
    inv = discover_and_parse_inventory(sample_ansible_project)

    # Verify hosts
    assert "web1.example.com" in inv.all_hosts
    assert "web2.example.com" in inv.all_hosts
    assert "db1.example.com" in inv.all_hosts
    assert "app1.example.com" in inv.all_hosts
    assert "app2.example.com" in inv.all_hosts
    assert "bastion.example.com" in inv.all_hosts

    # Verify groups
    assert "webservers" in inv.all_groups
    assert "dbservers" in inv.all_groups
    assert "appservers" in inv.all_groups
    assert "production" in inv.all_groups
    assert "all" in inv.all_groups

    # Verify direct host-to-group relationships
    assert inv.group_hosts["webservers"] == {"web1.example.com", "web2.example.com"}
    assert inv.group_hosts["dbservers"] == {"db1.example.com"}
    assert inv.group_hosts["appservers"] == {"app1.example.com", "app2.example.com"}
    assert "bastion.example.com" in inv.group_hosts["ungrouped"]

    # Verify parent-child group hierarchy
    assert inv.group_children["production"] == {"webservers", "appservers", "dbservers"}
    assert "production" in inv.group_parents["webservers"]
    assert "production" in inv.group_parents["appservers"]
    assert "production" in inv.group_parents["dbservers"]

    # Verify recursive hosts for parent group
    prod_hosts = inv.get_recursive_hosts("production")
    assert "web1.example.com" in prod_hosts
    assert "web2.example.com" in prod_hosts
    assert "app1.example.com" in prod_hosts
    assert "db1.example.com" in prod_hosts


def test_yaml_inventory_parsing(tmp_path: Path):
    yaml_inv_file = tmp_path / "inventory.yaml"
    yaml_inv_file.write_text(
        """
all:
  children:
    frontend:
      hosts:
        fe1.example.com:
          port: 80
        fe2.example.com:
    backend:
      children:
        api:
          hosts:
            api1.example.com:
            api2.example.com:
""".strip(),
        encoding="utf-8",
    )

    inv = InventoryData()
    parse_yaml_inventory(yaml_inv_file, inv)
    inv.finalize()

    assert inv.group_hosts["frontend"] == {"fe1.example.com", "fe2.example.com"}
    assert inv.group_hosts["api"] == {"api1.example.com", "api2.example.com"}
    assert "api" in inv.group_children["backend"]
    assert "backend" in inv.group_parents["api"]

    # Recursive hosts for backend includes api hosts
    assert inv.get_recursive_hosts("backend") == {"api1.example.com", "api2.example.com"}


def test_rendered_inventory_markdown_content(sample_ansible_project: Path):
    inv = discover_and_parse_inventory(sample_ansible_project)

    mock_targeting = [
        {"playbook": "site.yml", "play": "Deploy web servers", "roles": ["web", "common"]}
    ]
    doc = render_inventory_group_document("webservers", inv, mock_targeting)

    assert doc["id"] == "ansible:group:webservers"
    assert doc["title"] == "Inventory Group: webservers"

    content = doc["content"]
    # Check that hosts are explicitly listed
    assert "web1.example.com" in content
    assert "web2.example.com" in content
    # Check that parent groups are listed
    assert "`production`" in content
    # Check that targeted playbooks are listed
    assert "Playbook `site.yml`" in content
    assert "Deploy web servers" in content
    # Check that applied roles are explicitly stated
    assert "Host `web1.example.com` gets the `web` role." in content
    assert "Host `web2.example.com` gets the `web` role." in content
