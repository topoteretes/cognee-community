"""Ansible playbook parser extracting plays, targets, roles, and tasks.

Parses playbooks safely with PyYAML without executing modules, running tasks,
or evaluating Jinja templating expressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .loader import safe_load_yaml_all


@dataclass
class PlayData:
    """Represents a single play in a playbook."""

    name: str
    hosts: str
    roles: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)


@dataclass
class PlaybookData:
    """Represents a parsed playbook file."""

    file_path: str
    plays: list[PlayData] = field(default_factory=list)


def _extract_role_name(role_entry: Any) -> str | None:
    """Extract role name from a role declaration (string or dictionary)."""
    if isinstance(role_entry, str):
        return role_entry.strip()
    if isinstance(role_entry, dict):
        name = role_entry.get("role") or role_entry.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _extract_task_info(task_dict: dict[str, Any], roles_list: list[str]) -> str | None:
    """Extract task name and check for dynamically included or imported roles."""
    if not isinstance(task_dict, dict):
        return None

    # Check for include_role or import_role
    role_keys = (
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    )
    for role_key in role_keys:
        if role_key in task_dict:
            target = task_dict[role_key]
            if isinstance(target, dict):
                r_name = target.get("name")
                if isinstance(r_name, str) and r_name.strip() and r_name.strip() not in roles_list:
                    roles_list.append(r_name.strip())
            elif isinstance(target, str) and target.strip() and target.strip() not in roles_list:
                roles_list.append(target.strip())

    name = task_dict.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    # If unnamed, identify by primary module action if available
    for key in task_dict:
        if key not in (
            "name",
            "when",
            "tags",
            "notify",
            "register",
            "ignore_errors",
            "become",
            "vars",
            "loop",
            "with_items",
        ):
            return f"Action: {key}"

    return None


def _parse_play(play_dict: dict[str, Any], play_index: int) -> PlayData | None:
    """Parse a single play dictionary."""
    if not isinstance(play_dict, dict):
        return None

    hosts_raw = play_dict.get("hosts")
    if not hosts_raw:
        # Not a standard play with hosts target
        return None

    if isinstance(hosts_raw, list):
        hosts_str = ", ".join(str(h) for h in hosts_raw)
    else:
        hosts_str = str(hosts_raw)

    name = play_dict.get("name") or f"Play {play_index}"

    roles: list[str] = []
    # Extract declared roles
    for r in play_dict.get("roles", []) or []:
        r_name = _extract_role_name(r)
        if r_name and r_name not in roles:
            roles.append(r_name)

    # Extract tasks
    tasks: list[str] = []
    for task_list_key in ("pre_tasks", "tasks", "post_tasks"):
        task_list = play_dict.get(task_list_key) or []
        if isinstance(task_list, list):
            for t in task_list:
                t_name = _extract_task_info(t, roles)
                if t_name and t_name not in tasks:
                    tasks.append(t_name)

    # Extract handlers
    handlers: list[str] = []
    handler_list = play_dict.get("handlers") or []
    if isinstance(handler_list, list):
        for h in handler_list:
            if isinstance(h, dict):
                h_name = h.get("name")
                if isinstance(h_name, str) and h_name.strip():
                    handlers.append(h_name.strip())

    return PlayData(
        name=name,
        hosts=hosts_str,
        roles=roles,
        tasks=tasks,
        handlers=handlers,
    )


def parse_playbook_file(file_path: Path, project_root: Path) -> PlaybookData | None:
    """Parse a single playbook file into PlaybookData.

    Returns None if the file is not an Ansible playbook.
    """
    docs = safe_load_yaml_all(file_path)
    if not docs:
        return None

    plays: list[PlayData] = []
    play_index = 1

    for doc in docs:
        if isinstance(doc, list):
            for item in doc:
                if isinstance(item, dict):
                    play = _parse_play(item, play_index)
                    if play:
                        plays.append(play)
                        play_index += 1
        elif isinstance(doc, dict):
            play = _parse_play(doc, play_index)
            if play:
                plays.append(play)
                play_index += 1

    if not plays:
        return None

    try:
        rel_path = str(file_path.relative_to(project_root).as_posix())
    except ValueError:
        rel_path = file_path.name

    return PlaybookData(file_path=rel_path, plays=plays)


def is_candidate_playbook_file(file_path: Path, project_root: Path) -> bool:
    """Check if file_path could be an Ansible playbook (not role, var, or inventory file)."""
    if file_path.suffix not in (".yml", ".yaml"):
        return False

    try:
        rel = file_path.relative_to(project_root).as_posix()
    except ValueError:
        rel = file_path.name

    # Exclude roles, vars, inventory, and CI configs
    parts = rel.split("/")
    excluded_dirs = (
        "roles",
        "group_vars",
        "host_vars",
        "inventory",
        "inventories",
        ".github",
        ".git",
    )
    if any(p in excluded_dirs for p in parts):
        return False
    return file_path.name not in ("galaxy.yml", "requirements.yml", ".ansible-lint", "meta.yml")


def discover_and_parse_playbooks(project_root: Path) -> list[PlaybookData]:
    """Discover and parse all playbooks in an Ansible project directory."""
    playbooks: list[PlaybookData] = []

    if project_root.is_file():
        pb = parse_playbook_file(project_root, project_root.parent)
        if pb:
            playbooks.append(pb)
        return playbooks

    if not project_root.is_dir():
        return []

    # Look for playbook candidates at root and in subfolders (like playbooks/)
    candidates = []
    for ext in ("*.yml", "*.yaml"):
        for path in project_root.glob(ext):
            if is_candidate_playbook_file(path, project_root):
                candidates.append(path)

    # Check playbooks/ folder if present
    pb_dir = project_root / "playbooks"
    if pb_dir.is_dir():
        for ext in ("*.yml", "*.yaml"):
            for path in pb_dir.rglob(ext):
                if is_candidate_playbook_file(path, project_root):
                    candidates.append(path)

    for path in sorted(set(candidates)):
        pb = parse_playbook_file(path, project_root)
        if pb:
            playbooks.append(pb)

    return playbooks
