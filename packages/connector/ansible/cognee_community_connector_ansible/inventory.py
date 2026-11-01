"""Ansible inventory parser supporting both INI and YAML inventory formats.

Extracts hosts, groups, host-to-group relationships, and group hierarchies
while ensuring secrets and variable values are strictly excluded.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .loader import safe_load_yaml

# Header pattern for INI sections: [group_name] or [group_name:children] or [group_name:vars]
_INI_SECTION_PATTERN = re.compile(r"^\[([a-zA-Z0-9_\-\.]+)(?::(children|vars))?\]$")


def expand_host_pattern(pattern: str) -> list[str]:
    """Expand simple Ansible host range patterns like web[01:03].example.com or host[a:c]."""
    num_match = re.search(r"\[([0-9]+):([0-9]+)\]", pattern)
    if num_match:
        start_str, end_str = num_match.groups()
        pad = len(start_str) if start_str.startswith("0") and len(start_str) > 1 else 0
        start = int(start_str)
        end = int(end_str)
        step = 1 if start <= end else -1
        expanded = []
        for i in range(start, end + step, step):
            num_fmt = f"{i:0{pad}d}" if pad else str(i)
            expanded.append(pattern[: num_match.start()] + num_fmt + pattern[num_match.end() :])
        return expanded

    alpha_match = re.search(r"\[([a-zA-Z]):([a-zA-Z])\]", pattern)
    if alpha_match:
        start_c, end_c = alpha_match.groups()
        start = ord(start_c)
        end = ord(end_c)
        step = 1 if start <= end else -1
        expanded = []
        for i in range(start, end + step, step):
            expanded.append(pattern[: alpha_match.start()] + chr(i) + pattern[alpha_match.end() :])
        return expanded

    return [pattern]


class InventoryData:
    """Structured representation of an Ansible inventory."""

    def __init__(self) -> None:
        self.group_hosts: dict[str, set[str]] = defaultdict(set)
        self.group_children: dict[str, set[str]] = defaultdict(set)
        self.group_parents: dict[str, set[str]] = defaultdict(set)
        self.host_groups: dict[str, set[str]] = defaultdict(set)
        self.all_hosts: set[str] = set()
        self.all_groups: set[str] = set()

    def add_host_to_group(self, host: str, group: str) -> None:
        """Add a host to a group and track bi-directional relationships."""
        clean_host = host.strip()
        clean_group = group.strip()
        if not clean_host or not clean_group:
            return
        self.group_hosts[clean_group].add(clean_host)
        self.host_groups[clean_host].add(clean_group)
        self.all_hosts.add(clean_host)
        self.all_groups.add(clean_group)
        # All hosts implicitly belong to group 'all'
        if clean_group != "all":
            self.group_hosts["all"].add(clean_host)
            self.host_groups[clean_host].add("all")
        self.all_groups.add("all")

    def add_child_group(self, parent: str, child: str) -> None:
        """Add a parent-child group relationship."""
        clean_parent = parent.strip()
        clean_child = child.strip()
        if not clean_parent or not clean_child or clean_parent == clean_child:
            return
        self.group_children[clean_parent].add(clean_child)
        self.group_parents[clean_child].add(clean_parent)
        self.all_groups.add(clean_parent)
        self.all_groups.add(clean_child)
        self.all_groups.add("all")

    def get_recursive_hosts(self, group: str) -> set[str]:
        """Return all hosts in group, including those from child groups recursively."""
        visited_groups: set[str] = set()
        hosts: set[str] = set()

        def _collect(grp: str) -> None:
            if grp in visited_groups:
                return
            visited_groups.add(grp)
            hosts.update(self.group_hosts.get(grp, set()))
            for child in self.group_children.get(grp, set()):
                _collect(child)

        _collect(group)
        return hosts

    def finalize(self) -> None:
        """Ensure consistency and calculate implied parents."""
        # Ensure all groups without parents (except 'all') are children of 'all'
        for group in list(self.all_groups):
            if group != "all" and not self.group_parents[group]:
                self.group_children["all"].add(group)
                self.group_parents[group].add("all")


def parse_ini_inventory(path: Path, inv: InventoryData) -> None:
    """Parse an INI-format inventory file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    current_group = "ungrouped"
    current_mode = "hosts"  # "hosts", "children", or "vars"

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        section_match = _INI_SECTION_PATTERN.match(line)
        if section_match:
            group_name = section_match.group(1)
            modifier = section_match.group(2)
            current_group = group_name
            inv.all_groups.add(group_name)
            if modifier == "children":
                current_mode = "children"
            elif modifier == "vars":
                current_mode = "vars"
            else:
                current_mode = "hosts"
            continue

        if current_mode == "vars":
            # Variable values must NEVER be ingested!
            continue

        if current_mode == "children":
            # Child group name
            child_name = line.split()[0].split("#")[0].strip()
            if child_name:
                inv.add_child_group(current_group, child_name)
            continue

        if current_mode == "hosts":
            # Host line: host_name followed by optional key=value vars which MUST NOT be ingested
            raw_host = line.split()[0].split("#")[0].strip()
            if raw_host:
                for expanded_host in expand_host_pattern(raw_host):
                    inv.add_host_to_group(expanded_host, current_group)


def _parse_yaml_group(group_name: str, group_data: Any, inv: InventoryData) -> None:
    """Recursively parse a group dictionary in a YAML inventory."""
    if not isinstance(group_data, dict):
        return

    inv.all_groups.add(group_name)

    # Process hosts in this group
    hosts = group_data.get("hosts")
    if isinstance(hosts, dict):
        for host in hosts:
            if isinstance(host, str):
                for expanded in expand_host_pattern(host):
                    inv.add_host_to_group(expanded, group_name)
    elif isinstance(hosts, list):
        for item in hosts:
            if isinstance(item, str):
                for expanded in expand_host_pattern(item):
                    inv.add_host_to_group(expanded, group_name)
            elif isinstance(item, dict):
                for host in item:
                    for expanded in expand_host_pattern(str(host)):
                        inv.add_host_to_group(expanded, group_name)

    # Process child groups
    children = group_data.get("children")
    if isinstance(children, dict):
        for child_name, child_data in children.items():
            inv.add_child_group(group_name, child_name)
            _parse_yaml_group(child_name, child_data, inv)
    elif isinstance(children, list):
        for child in children:
            if isinstance(child, str):
                inv.add_child_group(group_name, child)


def parse_yaml_inventory(path: Path, inv: InventoryData) -> None:
    """Parse a YAML-format inventory file."""
    data = safe_load_yaml(path)
    if not isinstance(data, dict):
        return

    # Check for standard 'all' root or top-level groups
    if "all" in data:
        _parse_yaml_group("all", data["all"], inv)
    else:
        for group_name, group_data in data.items():
            if isinstance(group_data, dict):
                _parse_yaml_group(group_name, group_data, inv)


def is_yaml_inventory(path: Path) -> bool:
    """Determine whether an inventory file is YAML or INI."""
    if path.suffix in (".yml", ".yaml"):
        return True
    if path.suffix == ".ini":
        return False
    # If no recognized extension, check if it safely parses as YAML mapping with inventory keys
    data = safe_load_yaml(path)
    if isinstance(data, dict):
        has_hosts_or_children = any(
            "hosts" in v or "children" in v for v in data.values() if isinstance(v, dict)
        )
        if "all" in data or has_hosts_or_children:
            return True
    return False


def discover_and_parse_inventory(project_path: Path) -> InventoryData:
    """Discover inventory files in an Ansible project and parse them into InventoryData."""
    inv = InventoryData()
    inventory_files: list[Path] = []

    # If project_path itself is a file, parse it directly
    if project_path.is_file():
        inventory_files.append(project_path)
    elif project_path.is_dir():
        # Check standard inventory filenames at root
        for name in (
            "inventory",
            "inventory.ini",
            "inventory.yml",
            "inventory.yaml",
            "hosts",
            "hosts.ini",
            "hosts.yml",
            "hosts.yaml",
        ):
            cand = project_path / name
            if cand.is_file():
                inventory_files.append(cand)

        # Check inventory/ or inventories/ directory
        for dir_name in ("inventory", "inventories"):
            inv_dir = project_path / dir_name
            if inv_dir.is_dir():
                for item in sorted(inv_dir.rglob("*")):
                    if item.is_file() and not item.name.startswith("."):
                        inventory_files.append(item)

    # Parse discovered inventory files (deduplicated)
    seen: set[str] = set()
    for file_path in inventory_files:
        canonical = str(file_path.resolve())
        if canonical in seen:
            continue
        seen.add(canonical)

        if is_yaml_inventory(file_path):
            parse_yaml_inventory(file_path, inv)
        else:
            parse_ini_inventory(file_path, inv)

    inv.finalize()
    return inv
