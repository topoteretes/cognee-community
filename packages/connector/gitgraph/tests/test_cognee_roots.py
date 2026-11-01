"""The autouse fixture must redirect cognee's storage roots into a temp dir."""

from pathlib import Path

from cognee.base_config import get_base_config


def test_cognee_roots_point_under_tmp(cognee_roots: Path) -> None:
    config = get_base_config()
    assert Path(config.system_root_directory).is_relative_to(cognee_roots)
    assert Path(config.data_root_directory).is_relative_to(cognee_roots)
