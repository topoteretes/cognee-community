from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from .support import clear_engines, configure_storage, install_ai, pin_environment

pin_environment()


@pytest_asyncio.fixture
async def storage(tmp_path, monkeypatch):
    from cognee.base_config import get_base_config

    previous = (
        get_base_config().data_root_directory,
        get_base_config().system_root_directory,
    )
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    import cognee_community_connector_airtable.airtable as connector

    monkeypatch.setattr(connector, "_REQUEST_INTERVAL", 0)
    with install_ai():
        await configure_storage(tmp_path)
        try:
            yield tmp_path
        finally:
            import cognee

            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)
            clear_engines()
            cognee.config.data_root_directory(str(Path(previous[0])))
            cognee.config.system_root_directory(str(Path(previous[1])))
