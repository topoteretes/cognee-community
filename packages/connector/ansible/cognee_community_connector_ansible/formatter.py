"""Format parsed Ansible project elements into rich Markdown documents for Cognee."""

from typing import Any

from cognee_community_connector_ansible.models import (
    AnsibleGroup,
    AnsibleHost,
    AnsiblePlaybook,
    AnsibleProject,
    AnsibleRole,
)


def format_inventory_group_to_markdown(group: AnsibleGroup) -> str:
    """Format an inventory group into a markdown document with explicit relations."""
    lines: list[str] = [
        f"# Ansible Inventory Group: `{group.name}`",
        "",
        "## Overview",
        f"- **Group Name**: `{group.name}`",
    ]

    if group.parents:
        parents_str = ", ".join(f"`{p}`" for p in sorted(group.parents))
        lines.append(f"- **Parent Groups**: {parents_str}")
        for p in sorted(group.parents):
            lines.append(f"  - Group `{group.name}` is a child of parent group `{p}`.")
    else:
        lines.append("- **Parent Groups**: None (Top-level group)")

    if group.children:
        children_str = ", ".join(f"`{c}`" for c in sorted(group.children))
        lines.append(f"- **Child Groups**: {children_str}")
        for c in sorted(group.children):
            lines.append(f"  - Group `{group.name}` contains child group `{c}`.")

    lines.append("")
    lines.append("## Member Hosts")
    if group.hosts:
        for host in sorted(group.hosts):
            lines.append(f"- Host `{host}` belongs to group `{group.name}`.")
    else:
        lines.append("- *No direct hosts assigned to this group.*")

    if group.variable_names:
        lines.append("")
        lines.append("## Configured Variable Keys")
        lines.append("*(Secret values are strictly excluded by security policy)*")
        for v in sorted(group.variable_names):
            lines.append(f"- Variable `{v}` is configured for group `{group.name}`.")

    return "\n".join(lines)


def format_host_to_markdown(host: AnsibleHost) -> str:
    """Format a host into a markdown document."""
    lines: list[str] = [
        f"# Ansible Host: `{host.name}`",
        "",
        "## Network & Connection",
        f"- **Host Name / FQDN**: `{host.name}`",
    ]
    if host.ansible_host:
        lines.append(f"- **Target IP / Connection Address**: `{host.ansible_host}`")
    if host.port:
        lines.append(f"- **SSH / Connection Port**: `{host.port}`")

    lines.append("")
    lines.append("## Group Memberships")
    if host.groups:
        for g in sorted(host.groups):
            lines.append(f"- Host `{host.name}` belongs to group `{g}`.")
    else:
        lines.append("- *Ungrouped host*")

    if host.variables:
        lines.append("")
        lines.append("## Host-Specific Parameters")
        for k in sorted(host.variables.keys()):
            lines.append(f"- Parameter `{k}` configured for host `{host.name}`.")

    return "\n".join(lines)


def format_playbook_to_markdown(playbook: AnsiblePlaybook) -> str:
    """Format a playbook into a relational markdown document."""
    lines: list[str] = [
        f"# Ansible Playbook: `{playbook.name}`",
        "",
        f"- **Relative Path**: `{playbook.relative_path}`",
        f"- **Plays Count**: {len(playbook.plays)}",
        "",
        "## Plays Execution Plan",
    ]

    for idx, play in enumerate(playbook.plays, start=1):
        lines.append(f"### Play {idx}: {play.name}")
        lines.append(f"- **Target Hosts Pattern**: `{play.hosts_pattern}`")
        lines.append(f"  - Play `{play.name}` targets hosts matching `{play.hosts_pattern}`.")

        if play.roles:
            roles_str = ", ".join(f"`{r}`" for r in play.roles)
            lines.append(f"- **Roles Executed**: {roles_str}")
            for r in play.roles:
                lines.append(
                    f"  - Play `{play.name}` executes role `{r}` on hosts `{play.hosts_pattern}`."
                )

        if play.tasks:
            lines.append("- **Tasks Defined**:")
            for t in play.tasks:
                mod_str = f" (via `{t.module}`)" if t.module else ""
                lines.append(f"  - Task: `{t.name}`{mod_str}")
                if t.files_touched:
                    files_str = ", ".join(f"`{f}`" for f in t.files_touched)
                    lines.append(f"    - Modifies configuration files: {files_str}")
                if t.notify:
                    handlers_str = ", ".join(f"`{h}`" for h in t.notify)
                    lines.append(f"    - Triggers handlers: {handlers_str}")

        lines.append("")

    return "\n".join(lines)


def format_role_to_markdown(role: AnsibleRole) -> str:
    """Format an Ansible role into a relational markdown document."""
    lines: list[str] = [
        f"# Ansible Role: `{role.name}`",
        "",
        f"- **Role Name**: `{role.name}`",
        f"- **Relative Path**: `{role.relative_path}`",
        "",
        "## Dependencies",
    ]

    if role.dependencies:
        for dep in sorted(role.dependencies):
            lines.append(f"- Role `{role.name}` depends on role `{dep}`.")
    else:
        lines.append("- *No upstream role dependencies declared.*")

    lines.append("")
    lines.append("## Handlers")
    if role.handlers:
        for h in sorted(role.handlers):
            lines.append(f"- Role `{role.name}` provides handler `{h}`.")
    else:
        lines.append("- *No notification handlers.*")

    lines.append("")
    lines.append("## Configuration Tasks & Touchpoints")
    if role.tasks:
        for t in role.tasks:
            mod_str = f" [`{t.module}`]" if t.module else ""
            lines.append(f"- Task: `{t.name}`{mod_str}")
            if t.files_touched:
                files_str = ", ".join(f"`{f}`" for f in t.files_touched)
                lines.append(f"  - Role `{role.name}` modifies configuration file: {files_str}")
    else:
        lines.append("- *No tasks defined.*")

    if role.variable_names:
        lines.append("")
        lines.append("## Exposed Defaults and Variables")
        lines.append("*(Values stripped for credential protection)*")
        for v in sorted(role.variable_names):
            lines.append(f"- Variable `{v}` is recognized by role `{role.name}`.")

    return "\n".join(lines)


def project_to_cognee_documents(project: AnsibleProject) -> list[dict[str, Any]]:
    """Convert an entire parsed AnsibleProject into individual Cognee document rows."""
    documents: list[dict[str, Any]] = []

    # 1. Inventory Groups
    for group_name, group in sorted(project.groups.items()):
        doc_id = f"ansible:group:{group_name}"
        title = f"Ansible Inventory Group: {group_name}"
        content = format_inventory_group_to_markdown(group)
        documents.append(
            {
                "id": doc_id,
                "name": title,
                "title": title,
                "content": content,
                "type": "inventory_group",
                "source": "ansible",
            }
        )

    # 2. Hosts
    for host_name, host in sorted(project.hosts.items()):
        doc_id = f"ansible:host:{host_name}"
        title = f"Ansible Host: {host_name}"
        content = format_host_to_markdown(host)
        documents.append(
            {
                "id": doc_id,
                "name": title,
                "title": title,
                "content": content,
                "type": "host",
                "source": "ansible",
            }
        )

    # 3. Playbooks
    for pb in sorted(project.playbooks, key=lambda x: x.relative_path):
        doc_id = f"ansible:playbook:{pb.relative_path}"
        title = f"Ansible Playbook: {pb.name}"
        content = format_playbook_to_markdown(pb)
        documents.append(
            {
                "id": doc_id,
                "name": title,
                "title": title,
                "content": content,
                "type": "playbook",
                "source": "ansible",
            }
        )

    # 4. Roles
    for role_name, role in sorted(project.roles.items()):
        doc_id = f"ansible:role:{role_name}"
        title = f"Ansible Role: {role_name}"
        content = format_role_to_markdown(role)
        documents.append(
            {
                "id": doc_id,
                "name": title,
                "title": title,
                "content": content,
                "type": "role",
                "source": "ansible",
            }
        )

    return documents
