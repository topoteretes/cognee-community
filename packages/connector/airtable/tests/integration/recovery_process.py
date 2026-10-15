"""One fresh-interpreter phase of staging-to-document recovery acceptance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from support import (
    Airtable,
    clear_engines,
    configure_storage,
    documents,
    install_ai,
    pin_environment,
    recall,
    store_snapshot,
    sync,
)

pin_environment()


async def main(root, phase):
    from cognee.modules.data.models import Data
    from sqlalchemy.ext.asyncio import AsyncSession

    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    with install_ai():
        await configure_storage(root, prune=phase == "fail")
        if phase == "fail":
            original_commit = AsyncSession.commit
            trips = []

            async def fail_data_commit(session):
                if any(isinstance(item, Data) for item in session.new | session.dirty):
                    trips.append(True)
                    raise RuntimeError("injected document commit outage")
                await original_commit(session)

            with patch.object(AsyncSession, "commit", fail_data_commit):
                try:
                    await sync(airtable, include_schema=False)
                except Exception:
                    pass
                else:
                    raise AssertionError("Document storage outage was swallowed")
            assert trips, "Failure must occur at actual Data commit after staging"
            assert await documents() == []
            # Read the actual persisted dlt database: staging succeeded even
            # though the downstream relational Data commit was rejected.
            import sqlite3

            staged = False
            for database in (root / "system").rglob("dlt_database_*"):
                if not database.is_file() or database.name.endswith(("-wal", "-shm")):
                    continue
                with sqlite3.connect(database) as connection:
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    for (table,) in tables:
                        if "airtable_documents" in table:
                            escaped = table.replace('"', '""')
                            staged |= any(
                                "BeatrizAnand" in content
                                for (content,) in connection.execute(
                                    f'SELECT content FROM "{escaped}"'
                                )
                            )
            assert staged, "Actual staging database must contain the un-ingested document"
        elif phase == "recover":
            # Same remote record and fresh factory. With persisted dlt state,
            # the connector emits no change: retained staging drives recovery.
            await sync(airtable, include_schema=False)
            assert len(await documents()) == 1
            assert "beatrizanand" in await recall("Who restored the Fenwick mill?")
            snapshot = await store_snapshot()
            assert "beatrizanand" in snapshot["graph"]
            assert "beatrizanand" in snapshot["vector"]
        else:
            raise AssertionError(phase)
        clear_engines()
    print(f"RECOVERY_PHASE_OK={phase}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
