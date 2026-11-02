# cognee-community-connector-ansible

Ansible data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your infrastructure inventory, playbooks, and roles into Cognee's knowledge graph.

## Features

- **Document-Mode Ingestion:** Declares `DOCUMENT_SOURCE_ATTR = "ansible"`, routing generated Markdown documents directly through Cognee's entity extraction and knowledge graph pipeline.
- **Zero Credentials & Pure Local Parsing:** Parses INI and YAML files directly using PyYAML and standard libraries. Does not require `ansible-core` or remote SSH connections.
- **Strict Credential Protection:**
  - Files containing `$ANSIBLE_VAULT` encrypted blocks are skipped.
  - Inline `!vault` tags are safely redacted as `[SECRET_VAULT_REDACTED]`.
  - Variable values from inventory and roles are stripped by default (`strip_var_values=True`), keeping only variable names.
- **Explicit Relational Modeling:** Generates relational statements ("Host `web1` belongs to group `webservers`", "Role `nginx` depends on role `common`", "Play `site` targets hosts `webservers`") for optimal LLM knowledge graph reasoning.
- **Full Snapshot & Forget-on-Delete:** `write_disposition="replace"` ensures decommissioned servers or removed playbooks drop out of staging and are automatically cleaned up by Cognee's orphan cleanup.
- **Standalone CLI (`cognee-ansible`):** Includes `parse`, `dump`, and `audit` commands.

## Installation

```bash
pip install cognee-community-connector-ansible
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_ansible import ansible_source


async def main():
    # 1. Connect local Ansible repository
    source = ansible_source(
        path="./ansible-repo",
        exclude_vault=True,
        strip_var_values=True,
    )

    # 2. Ingest into memory
    await cognee.remember(source, dataset_name="infrastructure")

    # 3. Cognify relationships
    await cognee.cognify(datasets=["infrastructure"])

    # 4. Ask infrastructure questions
    answer = await cognee.search(
        "Which hosts receive the nginx role?",
        datasets=["infrastructure"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

## CLI Usage

```bash
# Inspect an Ansible project
cognee-ansible parse ./ansible-repo

# Export generated Cognee markdown documents
cognee-ansible dump ./ansible-repo -o ./output_docs

# Audit project for secret leaks
cognee-ansible audit ./ansible-repo
```

## Running Tests

```bash
pytest packages/connector/ansible/tests -v
```
