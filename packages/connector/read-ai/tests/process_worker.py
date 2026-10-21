"""Fresh-process sync worker, fault injection only at completed stage boundaries."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

os.environ["COGNEE_LOG_FILE"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"

EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "offline.py"
spec = importlib.util.spec_from_file_location("read_ai_offline_worker", EXAMPLE_PATH)
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


async def run(options):
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    from cognee_community_connector_read_ai import compat, read_ai_source, sync_read_ai

    root = Path(options.root)
    offline.configure_storage(root)
    models = offline.SyntheticModels().install()
    rows = json.loads(Path(options.meetings).read_text())
    provider = offline.SyntheticReadAI(rows)
    if options.denied:
        provider.denied.update(row["id"] for row in rows)
    if options.crash:
        original = getattr(compat, options.crash)

        async def crash(*args, **kwargs):
            # Let production code finish this phase, then simulate an abrupt restart
            # before the caller can acknowledge it in its durable journal.
            await original(*args, **kwargs)
            os._exit(77)

        setattr(compat, options.crash, crash)
    try:
        source = read_ai_source(
            connection_id="restart",
            token_provider=provider,
            client=provider.client,
            state_dir=root / "connector",
        )
        result = await sync_read_ai(source, dataset_name="read_ai_restart")
        user = await get_default_user()
        datasets = await get_authorized_existing_datasets(
            user=user, permission_type="write", datasets=["read_ai_restart"]
        )
        records = await get_dataset_data(datasets[0].id)
        owned = {
            row.system_metadata["external_id"]: str(row.id)
            for row in records
            if isinstance(row.system_metadata, dict)
            and row.system_metadata.get("source") == "read_ai"
        }
        print(
            "WORKER_RESULT="
            + json.dumps(
                {
                    "generation": result.generation,
                    "records": owned,
                    "extractions": models.extractions,
                    "embeddings": models.embeddings,
                }
            )
        )
    finally:
        provider.client.close()
        models.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("meetings")
    parser.add_argument("--crash", choices=["add_rows", "cognify", "verify_corpus"])
    parser.add_argument("--denied", action="store_true")
    asyncio.run(run(parser.parse_args()))
