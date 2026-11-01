"""Generates small, semantically meaningful Markdown documents from parsed Ansible project data.

Explicitly states relationships between inventory groups, hosts, playbooks,
and roles to optimize for semantic retrieval and knowledge graph construction.
"""

from __future__ import annotations

from typing import Any

from .inventory import InventoryData
from .playbook import PlaybookData
from .role import RoleData


def render_role_document(
    role: RoleData,
    playbook_references: list[dict[str, str]],
) -> dict[str, str]:
    """Generate a Markdown document for an Ansible role."""
    lines: list[str] = [
        f"# Role: {role.name}",
        "",
        "## Summary",
        f"Ansible role `{role.name}` with {len(role.tasks)} task(s) and "
        f"{len(role.handlers)} handler(s).",
        "",
    ]

    lines.append("## Tasks")
    if role.tasks:
        for t in role.tasks:
            lines.append(f"- {t}")
    else:
        lines.append("- None defined")
    lines.append("")

    lines.append("## Handlers")
    if role.handlers:
        for h in role.handlers:
            lines.append(f"- {h}")
    else:
        lines.append("- None defined")
    lines.append("")

    lines.append("## Dependencies")
    if role.dependencies:
        for dep in role.dependencies:
            lines.append(f"- Role `{role.name}` depends on `{dep}`.")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Default Variables")
    if role.default_vars:
        for var in role.default_vars:
            lines.append(f"- `{var}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Used By")
    if playbook_references:
        for ref in playbook_references:
            lines.append(
                f"- Playbook: `{ref['playbook']}` (Play: `{ref['play']}`, "
                f"Targets: `{ref['targets']}`)"
            )
    else:
        lines.append("- None")

    return {
        "id": f"ansible:role:{role.name}",
        "title": f"Role: {role.name}",
        "content": "\n".join(lines).strip(),
        "url": f"roles/{role.name}",
    }


def render_playbook_document(
    playbook: PlaybookData,
) -> dict[str, str]:
    """Generate a Markdown document for an Ansible playbook."""
    lines: list[str] = [
        f"# Playbook: {playbook.file_path}",
        "",
        "## Summary",
        f"Ansible playbook `{playbook.file_path}` defining {len(playbook.plays)} play(s).",
        "",
        "## Plays",
    ]

    all_targets: set[str] = set()
    all_roles: set[str] = set()

    for play in playbook.plays:
        lines.append(f"### Play: {play.name}")
        lines.append(f"- **Targets:** `{play.hosts}`")
        all_targets.add(play.hosts)

        if play.roles:
            roles_str = ", ".join(f"`{r}`" for r in play.roles)
            lines.append(f"- **Roles:** {roles_str}")
            all_roles.update(play.roles)
        else:
            lines.append("- **Roles:** None")

        if play.tasks:
            lines.append("- **Tasks:**")
            for t in play.tasks:
                lines.append(f"  - {t}")

        if play.handlers:
            lines.append("- **Handlers:**")
            for h in play.handlers:
                lines.append(f"  - {h}")

        lines.append("")

    lines.append("## Explicit Relationships")
    for play in playbook.plays:
        if play.roles:
            roles_str = ", ".join(f"`{r}`" for r in play.roles)
            lines.append(
                f"- Play `{play.name}` targets group/hosts `{play.hosts}` and "
                f"applies roles: {roles_str}."
            )
        else:
            lines.append(f"- Play `{play.name}` targets group/hosts `{play.hosts}`.")

    return {
        "id": f"ansible:playbook:{playbook.file_path}",
        "title": f"Playbook: {playbook.file_path}",
        "content": "\n".join(lines).strip(),
        "url": playbook.file_path,
    }


def render_inventory_group_document(
    group_name: str,
    inv: InventoryData,
    targeting_plays: list[dict[str, Any]],
) -> dict[str, str]:
    """Generate a Markdown document for an inventory group."""
    direct_hosts = sorted(inv.group_hosts.get(group_name, set()))
    recursive_hosts = sorted(inv.get_recursive_hosts(group_name))
    parents = sorted(inv.group_parents.get(group_name, set()))
    children = sorted(inv.group_children.get(group_name, set()))

    lines: list[str] = [
        f"# Inventory Group: {group_name}",
        "",
        "## Summary",
        f"Ansible inventory group `{group_name}` contains {len(direct_hosts)} direct host(s) "
        f"and {len(recursive_hosts)} total host(s).",
        "",
        "## Hosts",
    ]

    if direct_hosts:
        for h in direct_hosts:
            lines.append(f"- {h}")
    else:
        lines.append("- None direct")
    lines.append("")

    lines.append("## Parent Groups")
    if parents:
        for p in parents:
            lines.append(f"- `{p}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Child Groups")
    if children:
        for c in children:
            lines.append(f"- `{c}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Targeted By Playbooks")
    applied_roles: set[str] = set()
    if targeting_plays:
        for tp in targeting_plays:
            roles_str = ", ".join(f"`{r}`" for r in tp["roles"]) if tp["roles"] else "None"
            lines.append(
                f"- Playbook `{tp['playbook']}` (Play: `{tp['play']}`, Roles: {roles_str})"
            )
            applied_roles.update(tp["roles"])
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Applied Roles")
    if applied_roles:
        for role in sorted(applied_roles):
            lines.append(f"- Role `{role}` is applied to hosts in group `{group_name}`.")
            if direct_hosts:
                for h in direct_hosts:
                    lines.append(f"  - Host `{h}` gets the `{role}` role.")
    else:
        lines.append("- None")

    return {
        "id": f"ansible:group:{group_name}",
        "title": f"Inventory Group: {group_name}",
        "content": "\n".join(lines).strip(),
        "url": "inventory",
    }


def generate_all_documents(
    inv: InventoryData,
    playbooks: list[PlaybookData],
    roles: list[RoleData],
) -> list[dict[str, str]]:
    """Cross-reference components and generate Markdown documents for all items."""
    documents: list[dict[str, str]] = []

    # Map role -> list of playbooks and plays using it
    role_to_plays: dict[str, list[dict[str, str]]] = {r.name: [] for r in roles}
    # Map group/pattern -> list of plays targeting it
    target_to_plays: dict[str, list[dict[str, Any]]] = {}

    for pb in playbooks:
        for play in pb.plays:
            play_ref = {
                "playbook": pb.file_path,
                "play": play.name,
                "targets": play.hosts,
                "roles": play.roles,
            }
            # Record role usage
            for role_name in play.roles:
                if role_name in role_to_plays:
                    role_to_plays[role_name].append(play_ref)

            # Record target patterns (e.g. "webservers", "all")
            targets = [t.strip() for t in play.hosts.replace(":", ",").split(",") if t.strip()]
            for target in targets:
                target_to_plays.setdefault(target, []).append(play_ref)

    # 1. Render all inventory groups
    for group_name in sorted(inv.all_groups):
        targeting = target_to_plays.get(group_name, [])
        # Also include plays targeting 'all' if group is not 'all'
        if group_name != "all" and "all" in target_to_plays:
            targeting = list(targeting) + [
                tp for tp in target_to_plays["all"] if tp not in targeting
            ]
        doc = render_inventory_group_document(group_name, inv, targeting)
        documents.append(doc)

    # 2. Render all playbooks
    for pb in playbooks:
        doc = render_playbook_document(pb)
        documents.append(doc)

    # 3. Render all roles
    for r in roles:
        doc = render_role_document(r, role_to_plays.get(r.name, []))
        documents.append(doc)

    return documents
