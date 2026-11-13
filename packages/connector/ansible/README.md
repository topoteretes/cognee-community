# cognee-community-connector-ansible

An Ansible data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest operational infrastructure knowledge (inventories, playbooks, roles) into AI memory.

It exposes a `dlt` source you hand to `cognee.remember(...)` or `cognee.add(...)`.
Inventory groups, playbooks, and roles are extracted and rendered into concise Markdown
documents with explicit relationships, flowing through Cognee's standard document-mode
pipeline into semantic search and knowledge graph construction.

## Features

- **Inventory**: INI and YAML inventory formats, host-to-group relationships, parent-child group hierarchies.
- **Playbooks**: Plays, target hosts/groups, applied roles, and task descriptions.
- **Roles**: Tasks, handlers, dependencies (`meta/main.yml`), and default variable names.
- **Security-First**:
  - Encrypted Ansible Vault files and inline `!vault` blocks are skipped.
  - Variable values from `group_vars/`, `host_vars/`, and `defaults/` are **never ingested** (only variable names are preserved where relevant).
  - No remote SSH connections or server access required.
  - Safe YAML parsing only (no Jinja evaluation, no task/module execution).
- **Snapshot Sync**: `write_disposition="replace"` ensures deleted roles or playbooks drop out of memory via Cognee's orphan cleanup.
- **Minimal Dependencies**: Requires only `PyYAML` — no `ansible-core` or runtime needed.

## Install

```bash
uv pip install cognee-community-connector-ansible
# or from this monorepo:
cd packages/connector/ansible && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_ansible import ansible_source

# 1. Ingest local Ansible repository
await cognee.remember(
    ansible_source("/path/to/ansible-project"),
    dataset_name="infrastructure",
)

# 2. Query infrastructure memory
answer = await cognee.search(
    query_text="Which hosts get the nginx role?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["infrastructure"],
)
print(answer)
```

## How sync + forget-on-delete work

The source operates as a **full snapshot**: `write_disposition="replace"` replaces
staging with the current state of the local Ansible project on each run.
When a playbook, role, or inventory group is deleted from the project, it drops out of
the snapshot and Cognee's `orphan_cleanup` removes it from graph, vector, and relational stores.
Unchanged documents keep stable IDs so they are not re-processed unnecessarily.

## Testing

```bash
uv run pytest tests/
```
