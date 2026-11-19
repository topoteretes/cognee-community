"""Data models for Ansible inventory, playbooks, and roles."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AnsibleHost:
    """Represents a single target host in the Ansible inventory."""

    name: str
    groups: list[str] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    port: int | None = None
    ansible_host: str | None = None


@dataclass
class AnsibleGroup:
    """Represents an inventory group containing hosts and child groups."""

    name: str
    hosts: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    variable_names: list[str] = field(default_factory=list)


@dataclass
class AnsibleTask:
    """Represents a single task in a playbook or role."""

    name: str
    module: str = ""
    notify: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)


@dataclass
class AnsiblePlay:
    """Represents a play within an Ansible playbook."""

    name: str
    hosts_pattern: str
    roles: list[str] = field(default_factory=list)
    tasks: list[AnsibleTask] = field(default_factory=list)
    variable_names: list[str] = field(default_factory=list)


@dataclass
class AnsiblePlaybook:
    """Represents an Ansible playbook file."""

    name: str
    relative_path: str
    plays: list[AnsiblePlay] = field(default_factory=list)


@dataclass
class AnsibleRole:
    """Represents an Ansible role with tasks, dependencies, and defaults."""

    name: str
    relative_path: str
    dependencies: list[str] = field(default_factory=list)
    tasks: list[AnsibleTask] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)
    variable_names: list[str] = field(default_factory=list)


@dataclass
class AnsibleProject:
    """Unified container for an entire parsed Ansible project."""

    root_path: str
    groups: dict[str, AnsibleGroup] = field(default_factory=dict)
    hosts: dict[str, AnsibleHost] = field(default_factory=dict)
    playbooks: list[AnsiblePlaybook] = field(default_factory=list)
    roles: dict[str, AnsibleRole] = field(default_factory=dict)
    vault_files_count: int = 0
