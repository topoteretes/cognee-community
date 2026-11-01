"""Safe YAML loading and Ansible Vault detection utilities.

Ensures that secrets and encrypted vault data are never evaluated, executed,
or ingested into Cognee memory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


class SafeAnsibleLoader(yaml.SafeLoader):
    """Safe YAML loader that safely ignores Ansible Vault nodes and custom tags."""

    pass


def _ignore_custom_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    """Safely ignore custom tags like !vault without evaluating or leaking content."""
    return None


def _scalar_custom_tag(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Any:
    """Safely construct scalar values for tags like !unsafe."""
    return loader.construct_scalar(node)


# Register constructors for custom Ansible YAML tags
SafeAnsibleLoader.add_constructor("!vault", _ignore_custom_tag)
SafeAnsibleLoader.add_constructor("!unsafe", _scalar_custom_tag)
# Wildcard for any other custom tags: ignore safely rather than crashing
SafeAnsibleLoader.add_multi_constructor("!", lambda loader, suffix, node: None)

# Regex to detect inline $ANSIBLE_VAULT blocks
_VAULT_HEADER = re.compile(r"\$ANSIBLE_VAULT;\d+\.\d+;\w+")


def is_vault_content(text: str) -> bool:
    """Return True if text starts with an Ansible Vault header or is encrypted."""
    stripped = text.strip()
    return bool(_VAULT_HEADER.match(stripped) or stripped.startswith("$ANSIBLE_VAULT"))


def is_vault_file(path: Path) -> bool:
    """Return True if the file at path is an Ansible Vault encrypted file."""
    if not path.is_file():
        return False
    try:
        with path.open("rb") as f:
            header = f.read(32)
            return header.startswith(b"$ANSIBLE_VAULT")
    except OSError:
        return False


def safe_load_yaml(path: Path) -> Any:
    """Safely load a single YAML document from a file.

    Returns None if the file is encrypted with Ansible Vault, malformed, or unreadable.
    """
    if is_vault_file(path):
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            if is_vault_content(content):
                return None
            return yaml.load(content, Loader=SafeAnsibleLoader)
    except (yaml.YAMLError, OSError, ValueError, RecursionError):
        return None


def safe_load_yaml_all(path: Path) -> list[Any]:
    """Safely load all YAML documents from a multi-document file.

    Returns an empty list if the file is encrypted with Ansible Vault or unreadable.
    """
    if is_vault_file(path):
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            if is_vault_content(content):
                return []
            docs = list(yaml.load_all(content, Loader=SafeAnsibleLoader))
            return [d for d in docs if d is not None]
    except (yaml.YAMLError, OSError, ValueError, RecursionError):
        return []
