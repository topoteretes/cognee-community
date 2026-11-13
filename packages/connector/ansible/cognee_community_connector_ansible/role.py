"""Ansible role parser extracting tasks, handlers, dependencies, and variable names.

Ensures that variable values from defaults, group_vars, and host_vars are never
ingested into memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .loader import safe_load_yaml, safe_load_yaml_all


@dataclass
class RoleData:
    """Represents a parsed Ansible role."""

    name: str
    tasks: list[str] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    default_vars: list[str] = field(default_factory=list)


def _extract_task_names_from_file(file_path: Path) -> list[str]:
    """Extract task or handler names from a YAML file."""
    docs = safe_load_yaml_all(file_path)
    names: list[str] = []

    for doc in docs:
        if isinstance(doc, list):
            for item in doc:
                if isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str) and name.strip():
                        clean = name.strip()
                        if clean not in names:
                            names.append(clean)
                    else:
                        # Unnamed task: identify by primary action if present
                        for k in item:
                            if k not in (
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
                                action_name = f"Action: {k}"
                                if action_name not in names:
                                    names.append(action_name)
                                break
    return names


def _extract_dependencies(meta_file: Path) -> list[str]:
    """Extract role dependencies from meta/main.yml."""
    data = safe_load_yaml(meta_file)
    if not isinstance(data, dict):
        return []

    deps_raw = data.get("dependencies") or []
    dependencies: list[str] = []

    if isinstance(deps_raw, list):
        for dep in deps_raw:
            if isinstance(dep, str) and dep.strip():
                clean = dep.strip()
                if clean not in dependencies:
                    dependencies.append(clean)
            elif isinstance(dep, dict):
                d_name = dep.get("role") or dep.get("name")
                if isinstance(d_name, str) and d_name.strip():
                    clean = d_name.strip()
                    if clean not in dependencies:
                        dependencies.append(clean)

    return dependencies


def _extract_variable_names(defaults_dir: Path) -> list[str]:
    """Extract ONLY the variable names (keys) from defaults/.

    Variable values are strictly omitted and never ingested.
    """
    var_names: set[str] = set()

    for ext in ("*.yml", "*.yaml"):
        for path in defaults_dir.rglob(ext):
            data = safe_load_yaml(path)
            if isinstance(data, dict):
                for key in data:
                    if isinstance(key, str) and key.strip():
                        var_names.add(key.strip())

    return sorted(var_names)


def parse_role_directory(role_dir: Path) -> RoleData:
    """Parse an individual Ansible role directory."""
    role_name = role_dir.name

    tasks: list[str] = []
    tasks_dir = role_dir / "tasks"
    if tasks_dir.is_dir():
        for ext in ("*.yml", "*.yaml"):
            for path in sorted(tasks_dir.glob(ext)):
                for t in _extract_task_names_from_file(path):
                    if t not in tasks:
                        tasks.append(t)

    handlers: list[str] = []
    handlers_dir = role_dir / "handlers"
    if handlers_dir.is_dir():
        for ext in ("*.yml", "*.yaml"):
            for path in sorted(handlers_dir.glob(ext)):
                for h in _extract_task_names_from_file(path):
                    if h not in handlers:
                        handlers.append(h)

    dependencies: list[str] = []
    meta_dir = role_dir / "meta"
    if meta_dir.is_dir():
        for meta_name in ("main.yml", "main.yaml"):
            meta_file = meta_dir / meta_name
            if meta_file.is_file():
                for dep in _extract_dependencies(meta_file):
                    if dep not in dependencies:
                        dependencies.append(dep)

    default_vars: list[str] = []
    defaults_dir = role_dir / "defaults"
    if defaults_dir.is_dir():
        default_vars = _extract_variable_names(defaults_dir)

    return RoleData(
        name=role_name,
        tasks=tasks,
        handlers=handlers,
        dependencies=dependencies,
        default_vars=default_vars,
    )


def discover_and_parse_roles(project_root: Path) -> list[RoleData]:
    """Discover and parse all roles in an Ansible project."""
    roles: list[RoleData] = []

    if not project_root.is_dir():
        return []

    # Check if project_root itself is a standalone role (e.g. has tasks/ and meta/ or tasks/)
    if (project_root / "tasks").is_dir() and not (project_root / "roles").is_dir():
        roles.append(parse_role_directory(project_root))
        return roles

    # Standard location: roles/ directory
    roles_dir = project_root / "roles"
    if roles_dir.is_dir():
        for item in sorted(roles_dir.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                roles.append(parse_role_directory(item))

    return roles
