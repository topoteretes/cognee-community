"""Command-line interface for the Cognee Ansible connector."""

import argparse
import json
import sys
from pathlib import Path

from cognee_community_connector_ansible.formatter import project_to_cognee_documents
from cognee_community_connector_ansible.parser import AnsibleParser

__version__ = "0.1.0"


def main(args: list[str] | None = None) -> int:
    """CLI entry point for cognee-ansible."""
    parser = argparse.ArgumentParser(
        prog="cognee-ansible",
        description="Cognee Ansible connector CLI — parse, inspect, and export Ansible repos.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: parse
    parse_cmd = subparsers.add_parser("parse", help="Parse and inspect an Ansible project")
    parse_cmd.add_argument("path", help="Path to Ansible repository root")
    parse_cmd.add_argument("--json", action="store_true", help="Output summary in JSON format")

    # Command: dump
    dump_cmd = subparsers.add_parser("dump", help="Dump generated Cognee markdown documents")
    dump_cmd.add_argument("path", help="Path to Ansible repository root")
    dump_cmd.add_argument("-o", "--output", help="Directory to save generated markdown files")

    # Command: audit
    audit_cmd = subparsers.add_parser("audit", help="Audit project for Ansible Vault secret safety")
    audit_cmd.add_argument("path", help="Path to Ansible repository root")

    # Command: version
    subparsers.add_parser("version", help="Show connector version")

    parsed = parser.parse_args(args)

    if parsed.command == "version":
        print(f"cognee-community-connector-ansible {__version__}")
        return 0

    if not parsed.command:
        parser.print_help()
        return 0

    target_path = Path(parsed.path).resolve()
    if not target_path.exists():
        print(f"Error: Path '{target_path}' does not exist.", file=sys.stderr)
        return 1

    ansible_parser = AnsibleParser(project_dir=target_path)
    project = ansible_parser.parse()

    if parsed.command == "parse":
        summary = {
            "root_path": project.root_path,
            "groups_count": len(project.groups),
            "hosts_count": len(project.hosts),
            "playbooks_count": len(project.playbooks),
            "roles_count": len(project.roles),
            "vault_files_detected": project.vault_files_count,
            "groups": sorted(project.groups.keys()),
            "hosts": sorted(project.hosts.keys()),
            "roles": sorted(project.roles.keys()),
        }
        if parsed.json:
            print(json.dumps(summary, indent=2))
        else:
            print("=== Ansible Project Summary ===")
            print(f"Root: {project.root_path}")
            print(f"Inventory Groups: {len(project.groups)}")
            print(f"Inventory Hosts: {len(project.hosts)}")
            print(f"Playbooks: {len(project.playbooks)}")
            print(f"Roles: {len(project.roles)}")
            print(f"Encrypted Vault Files Skipped: {project.vault_files_count}")
        return 0

    if parsed.command == "dump":
        docs = project_to_cognee_documents(project)
        if parsed.output:
            out_dir = Path(parsed.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            for d in docs:
                safe_name = d["id"].replace(":", "_").replace("/", "_") + ".md"
                (out_dir / safe_name).write_text(d["content"], encoding="utf-8")
            print(f"Exported {len(docs)} documents to {out_dir}")
        else:
            for d in docs:
                print(f"--- Document: {d['id']} ({d['title']}) ---")
                print(d["content"])
                print()
        return 0

    if parsed.command == "audit":
        print(f"Auditing Ansible project at {target_path}...")
        docs = project_to_cognee_documents(project)
        leaked_vault = False
        for d in docs:
            if "$ANSIBLE_VAULT" in d["content"]:
                print(f"SECURITY ALERT: Raw $ANSIBLE_VAULT header found in {d['id']}!")
                leaked_vault = True
        if not leaked_vault:
            print(f"Audit PASSED: {len(docs)} documents verified. Zero vault secrets leaked.")
            return 0
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
