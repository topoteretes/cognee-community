"""Robust YAML and INI parser for Ansible inventory, playbooks, and roles."""

import contextlib
import re
from pathlib import Path
from typing import Any

import yaml

from cognee_community_connector_ansible.models import (
    AnsibleGroup,
    AnsibleHost,
    AnsiblePlay,
    AnsiblePlaybook,
    AnsibleProject,
    AnsibleRole,
    AnsibleTask,
)

# Regex to detect Ansible Vault header.
_VAULT_HEADER_PATTERN = re.compile(r"^\$ANSIBLE_VAULT;\d+\.\d+;[A-Z0-9]+", re.MULTILINE)

# Files touched parameters in common Ansible modules.
_FILE_PATH_KEYS = {"dest", "path", "src", "target", "file"}


class SafeVaultConstructor(yaml.SafeLoader):
    """Custom YAML loader that replaces !vault and other Ansible tags safely."""

    pass


def _vault_constructor(loader: yaml.Loader, node: yaml.Node) -> str:
    """Safely redact any !vault tag in YAML files."""
    return "[SECRET_VAULT_REDACTED]"


SafeVaultConstructor.add_constructor("!vault", _vault_constructor)
SafeVaultConstructor.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _safe_yaml_load(content: str) -> Any:
    """Safely parse YAML content using SafeVaultConstructor (SafeLoader subclass)."""
    return yaml.load(content, Loader=SafeVaultConstructor)  # nosec B506


class AnsibleParser:
    """Parses an Ansible repository root without requiring ansible-core."""

    def __init__(
        self,
        project_dir: str | Path,
        exclude_vault: bool = True,
        strip_var_values: bool = True,
    ) -> None:
        self.root_dir = Path(project_dir).resolve()
        self.exclude_vault = exclude_vault
        self.strip_var_values = strip_var_values

    def parse(self) -> AnsibleProject:
        """Parse inventories, playbooks, and roles into an AnsibleProject."""
        project = AnsibleProject(root_path=str(self.root_dir))

        if not self.root_dir.is_dir():
            return project

        # 1. Parse inventories
        self._discover_and_parse_inventories(project)

        # 2. Parse roles
        self._discover_and_parse_roles(project)

        # 3. Parse playbooks
        self._discover_and_parse_playbooks(project)

        return project

    def _discover_and_parse_inventories(self, project: AnsibleProject) -> None:
        """Find and parse inventory files in the project."""
        inventory_candidates: list[Path] = []

        # Standard file names
        for name in [
            "hosts",
            "hosts.ini",
            "inventory",
            "inventory.ini",
            "inventory.yml",
            "inventory.yaml",
            "production.ini",
            "staging.ini",
        ]:
            candidate = self.root_dir / name
            if candidate.is_file():
                inventory_candidates.append(candidate)

        # Inventories directory
        inv_dir = self.root_dir / "inventory"
        if inv_dir.is_dir():
            for child in inv_dir.iterdir():
                if child.is_file() and not child.name.startswith("."):
                    inventory_candidates.append(child)

        for inv_file in set(inventory_candidates):
            if self._is_vault_file(inv_file):
                project.vault_files_count += 1
                if self.exclude_vault:
                    continue
            self._parse_inventory_file(inv_file, project)

    def _parse_inventory_file(self, file_path: Path, project: AnsibleProject) -> None:
        """Parse single inventory file (either INI or YAML)."""
        content = file_path.read_text(encoding="utf-8", errors="replace")

        if content.strip().startswith(("{", "---", "all:")):
            self._parse_yaml_inventory(content, project)
        else:
            self._parse_ini_inventory(content, project)

    def _parse_ini_inventory(self, content: str, project: AnsibleProject) -> None:
        """Parse INI-style Ansible inventory."""
        current_group: str | None = None
        group_type = "hosts"  # hosts, children, vars

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue

            # Header match: [group] or [group:children] or [group:vars]
            if line.startswith("[") and line.endswith("]"):
                header = line[1:-1].strip()
                if ":" in header:
                    parts = header.split(":", 1)
                    current_group = parts[0].strip()
                    group_type = parts[1].strip()
                else:
                    current_group = header
                    group_type = "hosts"

                if current_group not in project.groups:
                    project.groups[current_group] = AnsibleGroup(name=current_group)
                continue

            if not current_group:
                current_group = "ungrouped"
                if current_group not in project.groups:
                    project.groups[current_group] = AnsibleGroup(name=current_group)

            group_obj = project.groups[current_group]

            if group_type == "children":
                child_group_name = line.split()[0].strip()
                if child_group_name not in group_obj.children:
                    group_obj.children.append(child_group_name)
                # Ensure child group exists and update parents
                if child_group_name not in project.groups:
                    project.groups[child_group_name] = AnsibleGroup(name=child_group_name)
                if current_group not in project.groups[child_group_name].parents:
                    project.groups[child_group_name].parents.append(current_group)

            elif group_type == "vars":
                var_key = line.split("=", 1)[0].strip()
                if var_key and var_key not in group_obj.variable_names:
                    group_obj.variable_names.append(var_key)

            elif group_type == "hosts":
                tokens = line.split()
                host_name = tokens[0].strip()
                if host_name not in group_obj.hosts:
                    group_obj.hosts.append(host_name)

                if host_name not in project.hosts:
                    project.hosts[host_name] = AnsibleHost(name=host_name)
                host_obj = project.hosts[host_name]
                if current_group not in host_obj.groups:
                    host_obj.groups.append(current_group)

                # Extract host parameters safely
                for token in tokens[1:]:
                    if "=" in token:
                        k, v = token.split("=", 1)
                        if k == "ansible_host":
                            host_obj.ansible_host = v
                        elif k == "ansible_port":
                            with contextlib.suppress(ValueError):
                                host_obj.port = int(v)
                        if not self.strip_var_values:
                            host_obj.variables[k] = v
                        else:
                            host_obj.variables[k] = "<STRIPPED>"

    def _parse_yaml_inventory(self, content: str, project: AnsibleProject) -> None:
        """Parse YAML-style Ansible inventory."""
        try:
            data = _safe_yaml_load(content)
        except (yaml.YAMLError, OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        def process_yaml_group(
            grp_name: str, grp_data: dict[str, Any], parent: str | None = None
        ) -> None:
            if grp_name not in project.groups:
                project.groups[grp_name] = AnsibleGroup(name=grp_name)
            group_obj = project.groups[grp_name]
            if parent and parent not in group_obj.parents:
                group_obj.parents.append(parent)

            # Hosts
            hosts_dict = grp_data.get("hosts")
            if isinstance(hosts_dict, dict):
                for h_name, h_vars in hosts_dict.items():
                    if h_name not in group_obj.hosts:
                        group_obj.hosts.append(h_name)
                    if h_name not in project.hosts:
                        project.hosts[h_name] = AnsibleHost(name=h_name)
                    if grp_name not in project.hosts[h_name].groups:
                        project.hosts[h_name].groups.append(grp_name)
                    if isinstance(h_vars, dict) and "ansible_host" in h_vars:
                        project.hosts[h_name].ansible_host = str(h_vars["ansible_host"])

            # Vars
            vars_dict = grp_data.get("vars")
            if isinstance(vars_dict, dict):
                for v_name in vars_dict:
                    if v_name not in group_obj.variable_names:
                        group_obj.variable_names.append(v_name)

            # Children
            children_dict = grp_data.get("children")
            if isinstance(children_dict, dict):
                for child_name, child_data in children_dict.items():
                    if child_name not in group_obj.children:
                        group_obj.children.append(child_name)
                    if isinstance(child_data, dict):
                        process_yaml_group(child_name, child_data, parent=grp_name)

        all_section = data.get("all")
        if isinstance(all_section, dict):
            process_yaml_group("all", all_section)

    def _discover_and_parse_roles(self, project: AnsibleProject) -> None:
        """Find and parse roles inside roles/ or standard paths."""
        roles_dir = self.root_dir / "roles"
        if not roles_dir.is_dir():
            return

        for role_path in roles_dir.iterdir():
            if not role_path.is_dir() or role_path.name.startswith("."):
                continue

            role_name = role_path.name
            rel_path = str(role_path.relative_to(self.root_dir)).replace("\\", "/")
            role_obj = AnsibleRole(name=role_name, relative_path=rel_path)

            # 1. Parse meta/main.yml for dependencies
            meta_file = role_path / "meta" / "main.yml"
            if not meta_file.is_file():
                meta_file = role_path / "meta" / "main.yaml"
            if meta_file.is_file():
                self._parse_role_meta(meta_file, role_obj)

            # 2. Parse defaults/main.yml and vars/main.yml for variable names
            for v_sub in ["defaults", "vars"]:
                v_file = role_path / v_sub / "main.yml"
                if not v_file.is_file():
                    v_file = role_path / v_sub / "main.yaml"
                if v_file.is_file():
                    self._parse_role_vars(v_file, role_obj)

            # 3. Parse tasks/main.yml
            tasks_file = role_path / "tasks" / "main.yml"
            if not tasks_file.is_file():
                tasks_file = role_path / "tasks" / "main.yaml"
            if tasks_file.is_file():
                role_obj.tasks = self._parse_tasks_file(tasks_file)

            # 4. Parse handlers/main.yml
            handlers_file = role_path / "handlers" / "main.yml"
            if not handlers_file.is_file():
                handlers_file = role_path / "handlers" / "main.yaml"
            if handlers_file.is_file():
                handlers = self._parse_tasks_file(handlers_file)
                role_obj.handlers = [h.name for h in handlers if h.name]

            project.roles[role_name] = role_obj

    def _parse_role_meta(self, meta_file: Path, role_obj: AnsibleRole) -> None:
        """Extract role dependencies from meta/main.yml."""
        try:
            content = meta_file.read_text(encoding="utf-8", errors="replace")
            data = _safe_yaml_load(content)
            if not isinstance(data, dict):
                return
            deps = data.get("dependencies") or []
            if isinstance(deps, list):
                for dep in deps:
                    if isinstance(dep, str):
                        role_obj.dependencies.append(dep)
                    elif isinstance(dep, dict) and "role" in dep:
                        role_obj.dependencies.append(str(dep["role"]))
        except (yaml.YAMLError, OSError, ValueError):
            return

    def _parse_role_vars(self, var_file: Path, role_obj: AnsibleRole) -> None:
        """Extract variable names without leaking values."""
        try:
            content = var_file.read_text(encoding="utf-8", errors="replace")
            data = _safe_yaml_load(content)
            if isinstance(data, dict):
                for k in data:
                    if k not in role_obj.variable_names:
                        role_obj.variable_names.append(str(k))
        except (yaml.YAMLError, OSError, ValueError):
            return

    def _discover_and_parse_playbooks(self, project: AnsibleProject) -> None:
        """Find and parse playbooks at project root and inside playbooks/."""
        playbook_files: list[Path] = []

        # Project root .yml / .yaml files
        for f in self.root_dir.glob("*.yml"):
            if not f.name.startswith("."):
                playbook_files.append(f)
        for f in self.root_dir.glob("*.yaml"):
            if not f.name.startswith("."):
                playbook_files.append(f)

        # playbooks/ directory
        pb_dir = self.root_dir / "playbooks"
        if pb_dir.is_dir():
            for f in pb_dir.glob("**/*.yml"):
                playbook_files.append(f)
            for f in pb_dir.glob("**/*.yaml"):
                playbook_files.append(f)

        for pb_file in set(playbook_files):
            if self._is_vault_file(pb_file):
                project.vault_files_count += 1
                if self.exclude_vault:
                    continue

            self._parse_playbook_file(pb_file, project)

    def _parse_playbook_file(self, pb_file: Path, project: AnsibleProject) -> None:
        """Parse a single playbook file containing plays."""
        try:
            content = pb_file.read_text(encoding="utf-8", errors="replace")
            data = _safe_yaml_load(content)
        except (yaml.YAMLError, OSError, ValueError):
            return

        if not isinstance(data, list):
            return

        rel_path = str(pb_file.relative_to(self.root_dir)).replace("\\", "/")
        playbook = AnsiblePlaybook(name=pb_file.name, relative_path=rel_path)

        for item in data:
            if not isinstance(item, dict) or "hosts" not in item:
                continue

            play_name = item.get("name", f"Play against {item['hosts']}")
            hosts_target = str(item["hosts"])
            roles_list: list[str] = []

            raw_roles = item.get("roles") or []
            if isinstance(raw_roles, list):
                for r in raw_roles:
                    if isinstance(r, str):
                        roles_list.append(r)
                    elif isinstance(r, dict) and "role" in r:
                        roles_list.append(str(r["role"]))

            # Tasks
            tasks_list: list[AnsibleTask] = []
            raw_tasks = item.get("tasks") or []
            if isinstance(raw_tasks, list):
                tasks_list = self._extract_tasks(raw_tasks)

            play = AnsiblePlay(
                name=play_name,
                hosts_pattern=hosts_target,
                roles=roles_list,
                tasks=tasks_list,
            )
            playbook.plays.append(play)

        if playbook.plays:
            project.playbooks.append(playbook)

    def _parse_tasks_file(self, task_file: Path) -> list[AnsibleTask]:
        """Parse tasks YAML file."""
        try:
            content = task_file.read_text(encoding="utf-8", errors="replace")
            data = _safe_yaml_load(content)
            if isinstance(data, list):
                return self._extract_tasks(data)
        except (yaml.YAMLError, OSError, ValueError):
            return []
        return []

    def _extract_tasks(self, raw_tasks: list[Any]) -> list[AnsibleTask]:
        """Extract AnsibleTask models from raw task list."""
        tasks: list[AnsibleTask] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue

            name = item.get("name", "Unnamed Task")
            notify_raw = item.get("notify") or []
            notify = [notify_raw] if isinstance(notify_raw, str) else list(notify_raw)
            tags_raw = item.get("tags") or []
            tags = [tags_raw] if isinstance(tags_raw, str) else list(tags_raw)

            # Identify module and files touched
            module = ""
            files: list[str] = []
            for k, v in item.items():
                if k in ("name", "notify", "tags", "when", "register", "become", "vars"):
                    continue
                # Likely the module name
                module = k
                if isinstance(v, dict):
                    for path_key in _FILE_PATH_KEYS:
                        if path_key in v:
                            files.append(str(v[path_key]))
                elif isinstance(v, str):
                    # e.g. dest=/etc/nginx/nginx.conf
                    for match in re.finditer(r"(?:dest|path|src)=([^\s]+)", v):
                        files.append(match.group(1))
                break

            tasks.append(
                AnsibleTask(
                    name=name,
                    module=module,
                    notify=notify,
                    tags=tags,
                    files_touched=files,
                )
            )
        return tasks

    def _is_vault_file(self, path: Path) -> bool:
        """Check if file starts with an Ansible Vault header."""
        try:
            with open(path, "rb") as f:
                prefix = f.read(64).decode("ascii", errors="ignore")
                return "$ANSIBLE_VAULT" in prefix
        except Exception:
            return False
