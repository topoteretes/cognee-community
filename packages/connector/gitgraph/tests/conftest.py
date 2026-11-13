"""Test setup: keep cognee's on-disk state out of the repo and the user's home."""

from collections.abc import Iterator
from pathlib import Path

import cognee
import pytest


@pytest.fixture(scope="session", autouse=True)
def cognee_roots(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point cognee's system and data roots at a temp dir before any test writes to cognee."""
    root = tmp_path_factory.mktemp("cognee")
    cognee.config.system_root_directory(str(root / "system"))
    cognee.config.data_root_directory(str(root / "data"))
    yield root
