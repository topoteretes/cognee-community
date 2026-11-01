"""Public connector-to-remember acceptance, including real deletion and recovery."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from .support import Airtable, documents, recall, store_snapshot, structured_output, sync


async def test_installed_release_environment(storage):
    import cognee
    import dlt
    import ladybug  # noqa: F401 -- required dependency, never silently skip storage coverage
    import lancedb  # noqa: F401

    assert importlib.metadata.version("cognee") == "1.6.3"
    assert importlib.metadata.version("dlt") == "1.30.0"
    assert "site-packages" in str(Path(cognee.__file__).resolve())
    assert "site-packages" in str(Path(dlt.__file__).resolve())


async def test_updates_comment_edits_and_shared_fact_deletion(storage):
    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill was restored by BeatrizAnand. SharedHarbor owns it.")
    airtable.comment("recMill", "The contact is DeclanObuya.")
    airtable.put("recClub", "The rowing club captain is CaseyPatel. SharedHarbor owns it.")
    await sync(airtable, include_schema=False)

    question = "Who restored the Fenwick mill, and who is its contact?"
    assert "beatrizanand" not in question.lower()
    assert "declanobuya" not in question.lower()
    retrieved = await recall(question)
    assert "beatrizanand" in retrieved
    assert "declanobuya" in retrieved
    answer = await recall(question, completion=True)
    assert "beatrizanand" in answer and "declanobuya" in answer
    before = await store_snapshot()
    assert "beatrizanand" in before["graph"]
    assert "declanobuya" in before["vector"]

    airtable.put("recMill", "Fenwick mill was restored by EmiliaStone. SharedHarbor owns it.")
    airtable.comment("recMill", "The contact is FarahSingh.")
    await sync(airtable, include_schema=False)
    retrieved = await recall(question)
    assert "emiliastone" in retrieved and "farahsingh" in retrieved
    assert "beatrizanand" not in retrieved and "declanobuya" not in retrieved
    updated = await store_snapshot()
    for fact in ("beatrizanand", "declanobuya"):
        assert fact not in updated["graph"]
        assert fact not in updated["vector"]

    airtable.remove("recMill")
    await sync(airtable, include_schema=False)
    surviving = await store_snapshot()
    assert "emiliastone" not in surviving["graph"]
    assert "farahsingh" not in surviving["vector"]
    assert "caseypatel" in surviving["graph"]
    assert "sharedharbor" in surviving["graph"]
    assert "sharedharbor" in surviving["vector"]
    assert "caseypatel" in await recall("Who is the rowing club captain?")
    assert len(await documents()) == 1


async def test_volatile_metadata_keeps_document_and_vector_identity(storage):
    airtable = Airtable()
    airtable.tables["tblPeople"]["fields"].append(
        {"id": "fldFiles", "name": "Files", "type": "multipleAttachments"}
    )
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    attachment = {
        "id": "attFixture",
        "filename": "manual.pdf",
        "type": "application/pdf",
        "size": 100,
        "url": "https://temporary.invalid/first",
        "thumbnails": {"small": {"url": "https://temporary.invalid/thumbnail"}},
    }
    airtable.records["tblPeople"]["recMill"]["fields"]["fldFiles"] = [attachment]
    await sync(airtable, include_schema=False)
    first = await documents()
    before = await store_snapshot()

    attachment["url"] = "https://temporary.invalid/refreshed"
    attachment["thumbnails"]["small"]["url"] = "https://temporary.invalid/new-thumbnail"
    airtable.records["tblPeople"]["recMill"]["fields"]["fldModified"] = "2026-02-01T00:00:00Z"
    await sync(airtable, include_schema=False)
    repeated = await documents()
    assert [(item.id, item.content_hash) for item in repeated] == [
        (item.id, item.content_hash) for item in first
    ]
    assert (await store_snapshot())["vector_ids"] == before["vector_ids"]
    assert "temporary.invalid" not in before["vector"]

    attachment["filename"] = "revised.pdf"
    await sync(airtable, include_schema=False)
    revised = await documents()
    assert revised[0].content_hash != repeated[0].content_hash
    assert "revised.pdf" in await recall("What file is attached to the mill record?")


async def test_base_table_and_dataset_isolation(storage):
    first = Airtable("appFirst")
    first.put("recSame", "Fenwick mill restorer is BeatrizAnand.")
    first.add_table("tblOther", "Other")
    first.put("recSame", "Rowing club captain is CaseyPatel.", table="tblOther")
    second = Airtable("appSecond")
    second.put("recSame", "The telescope inventor is EmiliaStone.")

    await sync(first, dataset="shared", include_schema=False)
    await sync(second, dataset="shared", include_schema=False)
    await sync(first, dataset="separate", include_schema=False)
    assert len(await documents("shared")) == 3
    assert len(await documents("separate")) == 2

    first.remove("recSame")
    await sync(first, dataset="shared", table_ids=["tblPeople"], include_schema=False)
    shared = await store_snapshot("shared")
    isolated = await store_snapshot("separate")
    assert "beatrizanand" not in shared["graph"]
    assert "caseypatel" in shared["graph"]  # deselected table remains
    assert "emiliastone" in shared["graph"]  # other base remains
    assert "beatrizanand" in isolated["graph"]  # other dataset remains
    assert "emiliastone" not in isolated["vector"]

    # Alternating datasets must keep independent staging/state cursors.
    await sync(first, dataset="separate", table_ids=["tblPeople"], include_schema=False)
    assert "beatrizanand" not in (await store_snapshot("separate"))["vector"]
    assert "caseypatel" in await recall("Who is the rowing club captain?", "separate")


async def test_deleted_table_removes_schema_and_all_records(storage):
    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    airtable.add_table("tblOther", "Other")
    airtable.put("recClub", "Rowing club captain is CaseyPatel.", table="tblOther")
    await sync(airtable)
    assert len(await documents()) == 4

    del airtable.tables["tblPeople"]
    await sync(airtable)
    remaining = await documents()
    assert len(remaining) == 2
    current = await store_snapshot()
    assert "beatrizanand" not in current["graph"]
    assert "beatrizanand" not in current["vector"]
    assert "caseypatel" in current["graph"]


async def test_failed_later_comment_fetch_publishes_no_partial_inventory(storage):
    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    airtable.add_table("tblOther", "Other")
    airtable.put("recClub", "Rowing club captain is CaseyPatel.", table="tblOther")
    await sync(airtable, include_schema=False)
    first = await documents()
    first_snapshot = await store_snapshot()

    airtable.put("recMill", "Fenwick mill restorer is EmiliaStone.")
    airtable.fail_comments = ("tblOther", "recClub")
    with pytest.raises(Exception, match="HTTP 403"):
        await sync(airtable, include_schema=False)
    assert {(item.id, item.content_hash) for item in await documents()} == {
        (item.id, item.content_hash) for item in first
    }
    assert (await store_snapshot())["vector_ids"] == first_snapshot["vector_ids"]
    assert "emiliastone" not in await recall("Who restored the Fenwick mill?")

    airtable.fail_comments = None
    await sync(airtable, include_schema=False)
    assert "emiliastone" in await recall("Who restored the Fenwick mill?")
    assert "beatrizanand" not in (await store_snapshot())["graph"]


async def test_unchanged_sync_recovers_failed_graph_processing(storage):
    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import KnowledgeGraph

    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    trips = []

    async def fail_graph(text_input=None, system_prompt=None, response_model=str, **kwargs):
        if response_model is KnowledgeGraph:
            trips.append(True)
            raise RuntimeError("injected graph extraction outage")
        return await structured_output(text_input, system_prompt, response_model, **kwargs)

    with patch.object(LLMGateway, "acreate_structured_output", fail_graph):
        with pytest.raises(Exception, match="injected graph extraction outage"):
            await sync(airtable, include_schema=False)
    assert trips, "Failure must occur after document ingestion, in graph processing"
    assert len(await documents()) == 1
    assert "beatrizanand" not in (await store_snapshot())["graph"]

    await sync(airtable, include_schema=False)
    assert "beatrizanand" in await recall("Who restored the Fenwick mill?")
    assert "beatrizanand" in (await store_snapshot())["graph"]


@pytest.mark.parametrize("delete_final_record", [False, True])
async def test_unchanged_sync_retries_failed_orphan_cleanup(storage, delete_final_record):
    from cognee.infrastructure.databases.graph.ladybug.adapter import LadybugAdapter

    airtable = Airtable()
    airtable.put("recMill", "Fenwick mill restorer is BeatrizAnand.")
    if not delete_final_record:
        airtable.put("recClub", "Rowing club captain is CaseyPatel.")
    await sync(airtable, include_schema=False)
    airtable.remove("recMill")
    trips = []

    async def fail_cleanup(*args, **kwargs):
        trips.append(True)
        raise RuntimeError("injected graph cleanup outage")

    # Fault only the backend mutation. Real Cognee orphan selection, provenance
    # planning, and retry logic all run. Cleanup is deliberately best-effort in
    # Cognee 1.6.3, so its first result may complete while retaining stale rows.
    with patch.object(LadybugAdapter, "delete_nodes", fail_cleanup):
        await sync(airtable, include_schema=False)
    assert trips, "The backend fault must actually interrupt orphan cleanup"
    assert "beatrizanand" in (await store_snapshot())["graph"]
    assert len(await documents()) == (1 if delete_final_record else 2)

    await sync(airtable, include_schema=False)
    after = await store_snapshot()
    assert "beatrizanand" not in after["graph"]
    assert "beatrizanand" not in after["vector"]
    assert len(await documents()) == (0 if delete_final_record else 1)
    if not delete_final_record:
        assert "caseypatel" in after["graph"]
        assert "caseypatel" in await recall("Who is the rowing club captain?")


def test_staged_documents_recover_in_a_fresh_process(tmp_path):
    script = Path(__file__).with_name("recovery_process.py")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    # Only the integration helpers are added, never an editable Cognee checkout.
    for phase in ("fail", "recover"):
        completed = subprocess.run(
            [sys.executable, str(script), str(tmp_path), phase],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stdout[-6000:] + completed.stderr[-6000:]
        assert f"RECOVERY_PHASE_OK={phase}" in completed.stdout
