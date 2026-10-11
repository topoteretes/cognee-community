"""Foreground sync using private local configuration and a dedicated runtime."""

import asyncio
import json
import os
import stat
from pathlib import Path

from local_runtime import configure_runtime
from verify_stores import assert_stores, inspect_stores

CONFIG = Path.home() / ".config/cognee-postman-4711/credentials.json"


def load_config():
    if CONFIG.is_symlink() or stat.S_IMODE(CONFIG.stat().st_mode) & 0o077:
        raise RuntimeError("Credential file must be private (chmod 600) and not a symlink.")
    return json.loads(CONFIG.read_text())


def optional_list(name):
    value = os.getenv(name)
    return None if value is None else [item.strip() for item in value.split(",") if item.strip()]


async def main():
    settings = load_config()
    selection = optional_list("POSTMAN_COLLECTION_UIDS")
    if not selection:
        raise RuntimeError(
            "Set POSTMAN_COLLECTION_UIDS to the synthetic collection UID(s) to ingest."
        )
    configure_runtime()
    import cognee

    from cognee_community_connector_postman import postman_source

    dataset = "postman_4711_demo"
    source = postman_source(
        api_key=settings["POSTMAN_API_KEY"],
        workspace_id=settings["POSTMAN_WORKSPACE_ID"],
        account_id=settings["POSTMAN_ACCOUNT_ID"],
        corpus_owner="local_postman_demo",
        dataset_name=dataset,
        collection_ids=selection,
        confirmed_deleted=optional_list("POSTMAN_CONFIRMED_DELETED_UIDS") or [],
        missing_collection_policy="mirror_visibility",
    )
    await cognee.add(
        source,
        dataset_name=dataset,
        run_in_background=False,
        dlt_config={"write_disposition": "replace", "primary_key": "id"},
    )
    await cognee.cognify(datasets=[dataset])
    results = await cognee.search(
        query_text="Describe the API requests and their documentation.",
        query_type=cognee.SearchType.CHUNKS,
        datasets=[dataset],
    )
    print(results)
    stats, texts = await inspect_stores(dataset)
    assert_stores(
        stats,
        texts,
        expect_empty=os.getenv("POSTMAN_EXPECT_EMPTY") == "1",
        forbidden=optional_list("POSTMAN_FORBIDDEN_TEXT") or [],
    )
    print(json.dumps(stats, indent=2))
    print("Store verification passed. Rerun after editing/deleting a request or collection.")


if __name__ == "__main__":
    asyncio.run(main())
