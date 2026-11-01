"""Ansible data-source connector for Cognee."""

from .ansible import ANSIBLE_SOURCE_NAME, ANSIBLE_TABLE_NAME, ansible_source, iter_ansible_documents

__all__ = [
    "ANSIBLE_SOURCE_NAME",
    "ANSIBLE_TABLE_NAME",
    "ansible_source",
    "iter_ansible_documents",
]
