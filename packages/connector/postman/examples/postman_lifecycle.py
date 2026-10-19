"""Opt-in synthetic Postman lifecycle with actual Cognee store verification.

Creates and deletes only its own two synthetic collections. Never executes requests.
"""

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
from local_runtime import configure_pipeline, configure_runtime
from postman_example import load_config
from verify_stores import assert_stores, inspect_stores


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-provider-writes",
        action="store_true",
        help="Authorize creating/editing/deleting synthetic collections.",
    )
    args = parser.parse_args()
    if not args.allow_provider_writes:
        parser.error("Synthetic lifecycle requires --allow-provider-writes.")
    default_runtime = (
        Path.home() / ".local/share/cognee-postman-4711" / ("lifecycle-" + uuid.uuid4().hex)
    )
    runtime = configure_runtime(Path(os.getenv("POSTMAN_RUNTIME_DIR", str(default_runtime))))
    try:
        (runtime / ".postman-lifecycle").touch(exist_ok=False)
    except FileExistsError:
        raise RuntimeError("Use a fresh POSTMAN_RUNTIME_DIR for each lifecycle run.") from None
    print(f"Lifecycle runtime: {runtime}", flush=True)
    import cognee

    from cognee_community_connector_postman import postman_source

    settings = load_config()
    owned, selection = [], []
    dataset = "postman_4711_lifecycle"
    report = []
    http = httpx.Client(
        base_url="https://api.postman.com",
        headers={
            "X-Api-Key": settings["POSTMAN_API_KEY"],
            "Accept": "application/json",
            "User-Agent": "cognee-postman-lifecycle/0.1",
        },
        timeout=30,
        follow_redirects=False,
    )

    def request(method, path, **kwargs):
        response = http.request(method, path, **kwargs)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Postman lifecycle HTTP {response.status_code}; response withheld.")
        return response.json()

    async def sync(label, rows, *, expected=(), forbidden=()):
        source = postman_source(
            api_key=settings["POSTMAN_API_KEY"],
            workspace_id=settings["POSTMAN_WORKSPACE_ID"],
            account_id=settings["POSTMAN_ACCOUNT_ID"],
            corpus_owner="local_postman_lifecycle",
            dataset_name=dataset,
            collection_ids=selection,
        )
        configure_pipeline(source, dataset, runtime)
        await cognee.add(
            source,
            dataset_name=dataset,
            run_in_background=False,
            dlt_config={"write_disposition": "replace", "primary_key": "id"},
        )
        await cognee.cognify(datasets=[dataset])
        stats, texts = await inspect_stores(dataset)
        assert_stores(
            stats,
            texts,
            expected_rows=rows,
            expect_empty=rows == 0,
            expected=expected,
            forbidden=forbidden,
        )
        if rows:
            if not stats["graph_nodes"] or not any(stats["vector_rows"].values()):
                raise RuntimeError("Expected populated graph and vectors.")
            results = await cognee.search(
                query_text="Where do the people live?",
                query_type=cognee.SearchType.CHUNKS,
                datasets=[dataset],
            )
            rendered = json.dumps(results, default=str).casefold()
            if any(token.casefold() not in rendered for token in expected):
                raise RuntimeError("Expected fixture content absent from chunk search.")
        entry = {"step": label, **stats}
        report.append(entry)
        print(json.dumps(entry), flush=True)

    try:
        for name, text in [
            ("Alpha request", "Aster lives in Oslo."),
            ("Beta request", "Borel lives in Bergen."),
        ]:
            tree = {
                "info": {
                    "name": "Cognee synthetic lifecycle 4711 " + uuid.uuid4().hex[:8],
                    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                },
                "item": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": name,
                        "request": {
                            "method": "GET",
                            "url": "https://example.invalid/never-run",
                            "description": text,
                        },
                    }
                ],
            }
            uid = request(
                "POST",
                "/collections",
                params={"workspace": settings["POSTMAN_WORKSPACE_ID"]},
                json={"collection": tree},
            )["collection"]["uid"]
            owned.append(uid)
            selection.append(uid)
        await sync("initial", 2, expected=["Oslo", "Bergen"])
        await sync("unchanged", 2, expected=["Oslo", "Bergen"])
        original_selection = list(selection)
        selection[:] = [original_selection[1]]
        await sync("selection_deselected", 1, expected=["Bergen"], forbidden=["Oslo"])
        selection[:] = original_selection
        await sync("selection_restored", 2, expected=["Oslo", "Bergen"])
        first = request("GET", "/collections/" + selection[0])["collection"]
        first["item"][0]["request"]["description"] = "Aster lives in Lima."
        request("PUT", "/collections/" + selection[0], json={"collection": first})
        await asyncio.sleep(4)
        await sync("edited", 2, expected=["Lima", "Bergen"], forbidden=["Oslo"])
        saved_items = first["item"]
        first["item"] = []
        first["info"]["description"] = "Orion lives in Tallinn."
        request("PUT", "/collections/" + selection[0], json={"collection": first})
        await asyncio.sleep(4)
        await sync("docs_only_collection", 2, expected=["Tallinn", "Bergen"], forbidden=["Lima"])
        first["info"]["description"] = ""
        request("PUT", "/collections/" + selection[0], json={"collection": first})
        await asyncio.sleep(4)
        await sync("final_request_deleted", 1, expected=["Bergen"], forbidden=["Lima", "Oslo"])
        first["item"] = saved_items
        request("PUT", "/collections/" + selection[0], json={"collection": first})
        await asyncio.sleep(4)
        await sync("request_restored", 2, expected=["Lima", "Bergen"])
        request("DELETE", "/collections/" + selection[0])
        owned.remove(selection[0])
        await sync(
            "nonempty_collection_deleted_next_sync",
            1,
            expected=["Bergen"],
            forbidden=["Lima", "Oslo"],
        )
        request("DELETE", "/collections/" + selection[1])
        owned.remove(selection[1])
        await sync("last_collection_deleted_next_sync", 0, forbidden=["Lima", "Bergen"])
        await sync("empty_replay", 0, forbidden=["Lima", "Bergen"])
        print("Synthetic lifecycle passed: search, edits, next-sync deletion and all-store checks.")
    finally:
        failed_cleanup = False
        for uid in owned:
            response = http.delete("/collections/" + uid)
            if response.status_code not in (200, 404):
                failed_cleanup = True
        http.close()
        if failed_cleanup:
            raise RuntimeError("Synthetic cleanup incomplete; inspect dedicated test workspace.")


if __name__ == "__main__":
    asyncio.run(main())
