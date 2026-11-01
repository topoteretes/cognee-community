"""Ansible connector demo — turn your Ansible repository into AI memory.

Ingests inventories, playbooks, and roles into cognee with full snapshot sync
and forget-on-delete. ``ansible_source`` returns a ``dlt`` source that routes
markdown documents through cognee's cognify entity extraction pipeline.

Run with:
    export LLM_API_KEY="sk-..."   # or other configured LLM provider
    uv run python examples/example.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Add package root to sys.path so the script runs directly without prior installation
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

import cognee  # noqa: E402

from cognee_community_connector_ansible import ansible_source  # noqa: E402

DATASET_NAME = "ansible_infra"


def create_demo_ansible_project(root: Path) -> None:
    """Create a sample Ansible project demonstrating inventories, roles, and playbooks."""
    # 1. Inventory
    inventory_content = """
[webservers]
web1.example.com
web2.example.com

[dbservers]
db1.example.com

[production:children]
webservers
dbservers
"""
    (root / "inventory.ini").write_text(inventory_content.strip())

    # 2. Roles
    # Common role
    common_tasks = root / "roles" / "common" / "tasks"
    common_tasks.mkdir(parents=True, exist_ok=True)
    (common_tasks / "main.yml").write_text(
        """
- name: Configure SSH daemon
  service:
    name: sshd
    state: started
- name: Setup NTP synchronization
  apt:
    name: ntp
    state: present
""".strip()
    )

    common_meta = root / "roles" / "common" / "meta"
    common_meta.mkdir(parents=True, exist_ok=True)
    (common_meta / "main.yml").write_text("dependencies: []\n")

    # Nginx role
    nginx_tasks = root / "roles" / "nginx" / "tasks"
    nginx_tasks.mkdir(parents=True, exist_ok=True)
    (nginx_tasks / "main.yml").write_text(
        """
- name: Install nginx package
  apt:
    name: nginx
    state: present
- name: Configure nginx virtual host
  template:
    src: vhost.j2
    dest: /etc/nginx/sites-available/default
- name: Ensure nginx service is running
  service:
    name: nginx
    state: started
""".strip()
    )

    nginx_meta = root / "roles" / "nginx" / "meta"
    nginx_meta.mkdir(parents=True, exist_ok=True)
    (nginx_meta / "main.yml").write_text(
        """
dependencies:
  - role: common
""".strip()
    )

    nginx_defaults = root / "roles" / "nginx" / "defaults"
    nginx_defaults.mkdir(parents=True, exist_ok=True)
    (nginx_defaults / "main.yml").write_text("http_port: 80\nserver_name: localhost\n")

    # 3. Playbook
    (root / "site.yml").write_text(
        """
- name: Setup base common configuration
  hosts: all
  roles:
    - common

- name: Deploy web servers with nginx
  hosts: webservers
  roles:
    - nginx
""".strip()
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "demo_ansible_repo"
        project_dir.mkdir()
        create_demo_ansible_project(project_dir)

        print(f"Ingesting Ansible repository from {project_dir} ...")
        source = ansible_source(project_dir)

        if not os.environ.get("LLM_API_KEY"):
            print("Note: LLM_API_KEY is not set.")
            print("Staging Ansible documents with cognee.add() ...")
            await cognee.add(source, dataset_name=DATASET_NAME)
            print("Successfully ingested documents into Cognee dataset via DLT document pipeline!")
            print("\nTo enable entity extraction and semantic graph search, set your API key:")
            print("    export LLM_API_KEY='your-key'")
            print("    uv run python examples/example.py")
            return

        await cognee.remember(source, dataset_name=DATASET_NAME)
        print("Ingestion complete.")

        queries = [
            "Which hosts get the nginx role?",
            "Which playbooks target the webservers group?",
            "Which roles depend on the common role?",
            "Which role configures SSH?",
            "Which tasks configure nginx?",
        ]

        for q in queries:
            print(f"\nQuery: {q}")
            answer = await cognee.search(
                query_text=q,
                query_type=cognee.SearchType.GRAPH_COMPLETION,
                datasets=[DATASET_NAME],
            )
            print(f"Answer:\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
