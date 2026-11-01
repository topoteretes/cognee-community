"""Security tests for Ansible connector.

Proves that:
- Ansible Vault files and inline !vault blocks are skipped.
- Variable values from group_vars/, host_vars/, and defaults/ NEVER leak into generated documents.
- Malformed YAML files are safely handled without crashing.
- Missing optional directories and empty projects do not fail.
"""

from __future__ import annotations

from pathlib import Path

from cognee_community_connector_ansible.ansible import iter_ansible_documents
from cognee_community_connector_ansible.loader import (
    is_vault_file,
    safe_load_yaml,
)


def test_vault_detection(sample_ansible_project: Path):
    vault_file = sample_ansible_project / "group_vars" / "all" / "vault.yml"
    assert is_vault_file(vault_file) is True
    assert safe_load_yaml(vault_file) is None


def test_inline_vault_tag_ignored(tmp_path: Path):
    yaml_with_vault = tmp_path / "inline_vault.yml"
    yaml_with_vault.write_text(
        """
api_user: test_user
api_secret: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  3665333538356134...
""".strip(),
        encoding="utf-8",
    )

    data = safe_load_yaml(yaml_with_vault)
    assert data is not None
    assert data.get("api_user") == "test_user"
    # Vault node safely ignored (None), never decrypted or evaluated
    assert data.get("api_secret") is None


def test_no_secret_values_leak_into_documents(sample_ansible_project: Path):
    """Verify that NO secret variable values appear in ANY generated document."""
    documents = list(iter_ansible_documents(sample_ansible_project))
    assert len(documents) > 0

    forbidden_secrets = [
        "LEAKY_HOST_TOKEN_123",
        "LEAKY_DB_PASSWORD_456",
        "LEAKY_GROUP_VAR_789",
        "SECRET_COMMON_VAL_999",
        "SECRET_WEB_VAL_888",
        "SECRET_APP_VAL_777",
        "ULTRA_SECRET_API_KEY_12345",
        "SUPER_SECRET_MASTER_PASSWORD_67890",
        "PRIVATE_KEY_SECRET_PLAINTEXT_DO_NOT_LEAK",
        "$ANSIBLE_VAULT",
        "366533353835613463326639366439366264353232373738",
    ]

    for doc in documents:
        content = doc["content"]
        title = doc["title"]
        for secret in forbidden_secrets:
            assert secret not in content, (
                f"Security violation: Secret '{secret}' found in document '{title}' content!"
            )
            assert secret not in title, (
                f"Security violation: Secret '{secret}' found in document title '{title}'!"
            )


def test_malformed_yaml_handled_gracefully(tmp_path: Path):
    malformed_pb = tmp_path / "broken_playbook.yml"
    malformed_pb.write_text(
        " - name: broken\n  hosts: [unbalanced\n    bad indent: %%%",
        encoding="utf-8",
    )

    # Safe loading should return None / empty list without unhandled exceptions
    assert safe_load_yaml(malformed_pb) is None
    docs = list(iter_ansible_documents(tmp_path))
    assert isinstance(docs, list)


def test_missing_optional_directories(tmp_path: Path):
    # A bare project with only an inventory file and no roles or playbooks
    (tmp_path / "hosts").write_text("[lonely_group]\nstandalone.example.com\n", encoding="utf-8")

    docs = list(iter_ansible_documents(tmp_path))
    assert len(docs) == 2  # lonely_group and all
    assert any(d["id"] == "ansible:group:lonely_group" for d in docs)


def test_empty_ansible_project(tmp_path: Path):
    # An empty directory produces 0 documents without crashing
    docs = list(iter_ansible_documents(tmp_path))
    assert docs == []
