"""Ansible data-source connector for Cognee (inventory, playbooks, roles).

Parses a local Ansible project directory (INI/YAML inventories, playbooks, and roles)
and produces structured Markdown documents declaring explicit infrastructure relationships
for ingestion into Cognee's knowledge graph.

Follows full-snapshot sync (write_disposition="replace") so obsolete playbooks or hosts
are automatically forgotten by Cognee's orphan cleanup.

Vault secrets ($ANSIBLE_VAULT blocks and !vault tags) are strictly excluded and variable
values are stripped by default to ensure zero credential leakage.
"""

from pathlib import Path

from cognee_community_connector_ansible.formatter import project_to_cognee_documents
from cognee_community_connector_ansible.parser import AnsibleParser

# Staging table name and source identifier.
ANSIBLE_TABLE_NAME = "ansible_documents"
ANSIBLE_SOURCE_NAME = "ansible"

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"


def ansible_source(
    path: str | Path,
    exclude_vault: bool = True,
    strip_var_values: bool = True,
):
    """Create a dlt source yielding Ansible configuration documents.

    Args:
        path: Path to the root directory of the Ansible project or repository.
        exclude_vault: When True, skips files with $ANSIBLE_VAULT headers and masks
            any !vault tags to prevent secrets ingestion. Defaults to True.
        strip_var_values: When True, retains only variable names and strips all
            values from inventory and roles to prevent accidental token exposure.
            Defaults to True.

    Returns:
        A dlt source yielding relational markdown documents for Cognee cognify.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError("The Ansible connector requires dlt: pip install dlt") from exc

    resolved_path = Path(path).resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Ansible project path not found: {resolved_path}")

    @dlt.resource(
        name=ANSIBLE_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def ansible_documents():
        """Yield Markdown documents representing the Ansible project."""
        parser = AnsibleParser(
            project_dir=resolved_path,
            exclude_vault=exclude_vault,
            strip_var_values=strip_var_values,
        )
        project = parser.parse()
        documents = project_to_cognee_documents(project)

        yield from documents

    @dlt.source(name=ANSIBLE_SOURCE_NAME)
    def _ansible():
        return ansible_documents

    source = _ansible()
    setattr(source, DOCUMENT_SOURCE_ATTR, ANSIBLE_SOURCE_NAME)
    return source
