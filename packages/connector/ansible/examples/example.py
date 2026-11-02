"""Ansible connector demo — sync infrastructure state and playbooks into Cognee memory.

Pulls local Ansible inventories, playbooks, and roles into Cognee knowledge graph.
``ansible_source`` returns a ``dlt`` source that routes directly to Cognee's
cognify pipeline as document entities.

Each run is a full snapshot: deleted hosts or retired playbooks drop out of the
snapshot and are cleaned up by Cognee's orphan cleanup.

Zero credential leakage:
- Files with $ANSIBLE_VAULT headers and !vault tags are strictly excluded.
- Variable values are stripped, keeping only variable keys.
"""

import asyncio
from pathlib import Path

import cognee

from cognee_community_connector_ansible import ansible_source

DATASET_NAME = "ansible_infrastructure"


async def main() -> None:
    # Point to your local Ansible repository or playbook directory
    ansible_project_path = Path("./ansible-repo")

    print(f"Connecting Ansible project at {ansible_project_path} to Cognee...")

    # 1. Initialize dlt source
    source = ansible_source(
        path=ansible_project_path,
        exclude_vault=True,
        strip_var_values=True,
    )

    # 2. Ingest into Cognee memory
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Ingested Ansible inventory, playbooks, and roles into memory.")

    # 3. Cognify relationships (graph entity extraction)
    await cognee.cognify(datasets=[DATASET_NAME])
    print("Cognify completed. Infrastructure graph is ready for queries.")

    # 4. Search infrastructure graph
    query = "Which servers get the nginx role and what configuration files are touched?"
    results = await cognee.search(query, datasets=[DATASET_NAME])
    print(f"\nQuery: {query}")
    print(f"Answer: {results}")


if __name__ == "__main__":
    asyncio.run(main())
