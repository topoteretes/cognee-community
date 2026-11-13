"""DLT source for Ansible projects (inventory, playbooks, roles).

Extracts operational infrastructure knowledge and yields markdown documents
into Cognee memory using full-snapshot sync (write_disposition="replace") and
document-mode tagging.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from .inventory import discover_and_parse_inventory
from .playbook import discover_and_parse_playbooks
from .renderer import generate_all_documents
from .role import discover_and_parse_roles

logger = get_logger("ansible_connector")

ANSIBLE_SOURCE_NAME = "ansible"
ANSIBLE_TABLE_NAME = "ansible_documents"


def iter_ansible_documents(project_path: Path) -> Iterator[dict[str, Any]]:
    """Discover and parse inventory, playbooks, and roles in project_path.

    Yields document rows suitable for Cognee ingestion.
    """
    if not project_path.exists():
        raise ValueError(f"Ansible project path does not exist: {project_path}")

    inv = discover_and_parse_inventory(project_path)
    playbooks = discover_and_parse_playbooks(project_path)
    roles = discover_and_parse_roles(project_path)

    documents = generate_all_documents(inv, playbooks, roles)
    logger.info(
        "Ansible connector: discovered %d group(s), %d host(s), %d playbook(s), %d role(s); "
        "generated %d document(s).",
        len(inv.all_groups),
        len(inv.all_hosts),
        len(playbooks),
        len(roles),
        len(documents),
    )

    yield from documents


def ansible_source(
    path: str | os.PathLike[str],
    resource_name: str = ANSIBLE_TABLE_NAME,
) -> Any:
    """Create a DLT source that yields Ansible project knowledge as markdown documents.

    Args:
        path: Path to the local Ansible project directory (or inventory file).
        resource_name: dlt resource/table name (default: "ansible_documents").

    Returns:
        A DLT source suitable for ``cognee.remember(...)`` or ``cognee.add(...)``.
        Marked as a document source so each document flows through normal cognify.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Ansible connector requires dlt: pip install "cognee[dlt]" or pip install dlt.'
        ) from exc

    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        raise ValueError(f"Ansible project path does not exist: {path}")

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="replace",
    )
    def ansible_documents() -> Iterator[dict[str, Any]]:
        yield from iter_ansible_documents(resolved_path)

    @dlt.source(name=ANSIBLE_SOURCE_NAME)
    def _ansible():
        return ansible_documents

    source = _ansible()
    setattr(source, DOCUMENT_SOURCE_ATTR, ANSIBLE_SOURCE_NAME)
    setattr(ansible_documents, DOCUMENT_SOURCE_ATTR, ANSIBLE_SOURCE_NAME)
    return source
