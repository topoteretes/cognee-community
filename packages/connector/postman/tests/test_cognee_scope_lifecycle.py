"""Opt-in real-store isolation test using local models and synthetic provider reads."""

import asyncio
import json
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.getenv("POSTMAN_MODEL_TEST") != "1", reason="Opt-in local-model Cognee integration test"
)
def test_forgetting_one_corpus_preserves_other_populated_corpus(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "support"))
    from local_runtime import configure_pipeline, configure_runtime
    from test_postman import FakeClient
    from verify_stores import assert_stores, inspect_stores

    runtime = configure_runtime(tmp_path / "cognee")
    import cognee

    from cognee_community_connector_postman import postman_source

    dataset = "postman_scope_test"
    left, right = FakeClient(), FakeClient()
    left.tree["item"] = left.tree["item"][:1]
    right.tree["item"] = right.tree["item"][:1]
    left.tree["item"][0]["request"]["description"] = "Ravi lives in Calgary."
    right.tree["item"][0]["request"]["description"] = "Yuna lives in Kyoto."

    async def sync(client, owner, selection=("1-c",)):
        src = postman_source(
            client=client,
            workspace_id="w",
            resource_name=owner,
            collection_ids=selection,
        )
        configure_pipeline(src, dataset, runtime)
        await cognee.remember(
            src,
            dataset_name=dataset,
            run_in_background=False,
            dlt_config={"write_disposition": "replace", "primary_key": "id"},
        )

    async def run():
        await sync(left, "left")
        await sync(right, "right")
        stats, texts = await inspect_stores(dataset)
        assert_stores(stats, texts, expected_rows=2, expected=["Calgary", "Kyoto"])
        await sync(left, "left", [])
        stats, texts = await inspect_stores(dataset)
        assert_stores(stats, texts, expected_rows=1, expected=["Kyoto"], forbidden=["Calgary"])
        assert stats["graph_nodes"] > 0
        results = await cognee.search(
            query_text="Where does Yuna live?",
            query_type=cognee.SearchType.CHUNKS,
            datasets=[dataset],
        )
        assert "Kyoto" in json.dumps(results, default=str)
        await sync(left, "left", [])
        stats, texts = await inspect_stores(dataset)
        assert_stores(stats, texts, expected_rows=1, expected=["Kyoto"], forbidden=["Calgary"])
        right.inventory = {}
        await sync(right, "right")
        stats, texts = await inspect_stores(dataset)
        assert_stores(stats, texts, expect_empty=True)

    asyncio.run(run())
