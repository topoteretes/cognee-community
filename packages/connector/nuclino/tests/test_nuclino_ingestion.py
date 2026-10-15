"""Integration tests for the Nuclino connector through Cognee's real ingestion layer.

Uses the real cognee.add() pipeline with the Nuclino REST API mocked (no live credentials,
no LLM calls). Follows the Google Drive connector integration test architecture.
"""

from __future__ import annotations

from typing import Any

import cognee
import dlt
import pytest
import pytest_asyncio
import requests
from cognee.infrastructure.files.utils.open_data_file import open_data_file
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, is_dlt_sourced
from cognee.tasks.ingestion.exceptions.exceptions import DLTIngestionError
from dlt.extract.exceptions import PipeException
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_nuclino import nuclino_source

DATASET_NAME = "nuclino_integration_test"


class FakeNuclinoResponse:
    """Mock requests.Response for Nuclino integration tests."""

    def __init__(
        self,
        payload: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = requests.Request("GET", "https://api.nuclino.com/v0/test").prepare()
            resp = requests.Response()
            resp.status_code = self.status_code
            resp.headers.update(self.headers)
            resp.request = req
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code} Error",
                response=resp,
            )


class MockNuclinoApiSession:
    """Mock HTTP session providing stateful responses for Nuclino REST API."""

    def __init__(
        self,
        *,
        workspaces: list[dict[str, Any]] | None = None,
        collections: dict[str, dict[str, Any]] | None = None,
        items: dict[str, dict[str, Any]] | None = None,
        fail_on_url: str | None = None,
    ):
        self.workspaces = list(workspaces or [{"id": "ws-1"}])
        self.collections = dict(collections or {})
        self.items = dict(items or {})
        self.fail_on_url = fail_on_url
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> FakeNuclinoResponse:
        self.calls.append((url, dict(params or {})))

        if self.fail_on_url and self.fail_on_url in url:
            raise RuntimeError(f"Simulated API failure on {url}")

        if url == "https://api.nuclino.com/v0/workspaces":
            return FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"results": self.workspaces},
                }
            )

        if url == "https://api.nuclino.com/v0/items":
            # Nuclino's /v0/items returns metadata for both collections and items
            all_objects = list(self.collections.values()) + list(self.items.values())
            results = [
                {
                    "id": obj["id"],
                    "workspaceId": obj.get("workspaceId", "ws-1"),
                    "object": obj.get("object", "item"),
                    "title": obj.get("title", ""),
                    "lastUpdatedAt": obj.get("lastUpdatedAt", "2026-01-01T00:00:00.000Z"),
                    "url": obj.get("url"),
                }
                for obj in all_objects
            ]
            return FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"results": results},
                }
            )

        if url.startswith("https://api.nuclino.com/v0/items/"):
            item_id = url.split("/")[-1]
            if item_id in self.items:
                return FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": self.items[item_id],
                    }
                )
            if item_id in self.collections:
                return FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": self.collections[item_id],
                    }
                )
            return FakeNuclinoResponse(status_code=404)

        return FakeNuclinoResponse(status_code=404)


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    """Set up a clean temporary Cognee test environment with SQLite and no live LLM calls."""
    pytest.importorskip("dlt")

    # add() never calls the LLM (no cognify()), but cognee's startup
    # connection check would still try to reach one — skip it so this test
    # needs no live credentials.
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    yield

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _get_all_dataset_data(dataset_name: str) -> list[Any]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return []
    return await get_dataset_data(datasets[0].id)


def _data_metadata(data_item: Any) -> dict[str, Any]:
    for attr in ("system_metadata", "external_metadata"):
        val = getattr(data_item, attr, None)
        if isinstance(val, dict):
            return val
    return {}


async def _get_nuclino_data(dataset_name: str) -> list[Any]:
    all_data = await _get_all_dataset_data(dataset_name)
    return [d for d in all_data if _data_metadata(d).get("source") == "nuclino"]


async def _read_data_text(data_item: Any) -> str:
    async with open_data_file(data_item.raw_data_location, mode="r", encoding="utf-8") as file:
        return file.read()


@pytest.mark.asyncio
async def test_nuclino_ingestion_incremental_resync_and_deletion(clean_environment):
    """Prove that Nuclino connector works through Cognee's real add() ingestion layer.

    Verifies:
      - Initial run stores document-mode records for collections and items.
      - System metadata correctly stores provenance (source="nuclino", external_id, title, url).
      - Stored document text contains '# {title}\\n\\n{content}'.
      - is_dlt_sourced() returns False for document-mode records.
      - Resync preserves unchanged objects with stable Cognee Data IDs.
      - Content-changed objects derive a new Cognee Data ID reflecting the new content hash.
      - Vanished objects are purged via DLT tombstone hard-delete and Cognee orphan cleanup.
      - Source scoping ensures non-Nuclino and other-source documents in the same
        dataset are preserved.
    """
    collection_1 = {
        "id": "collection-1",
        "workspaceId": "ws-1",
        "object": "collection",
        "title": "Collection One",
        "content": "Markdown content of collection one.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
        "url": "https://share.nuclino.com/c/collection-1",
    }
    item_1 = {
        "id": "item-1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item One",
        "content": "Markdown content of item one.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
        "url": "https://share.nuclino.com/i/item-1",
    }
    item_2 = {
        "id": "item-2",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item Two",
        "content": "Markdown content of item two.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
        "url": "https://share.nuclino.com/i/item-2",
    }

    mock_session = MockNuclinoApiSession(
        workspaces=[{"id": "ws-1"}],
        collections={"collection-1": collection_1},
        items={"item-1": item_1, "item-2": item_2},
    )

    # -----------------------------------------------------------------------
    # Run 1: Initial ingestion
    # -----------------------------------------------------------------------
    await cognee.add(
        nuclino_source(
            api_key="test-api-key",
            workspace_ids=["ws-1"],
            session=mock_session,
        ),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    initial_nuclino_data = await _get_nuclino_data(DATASET_NAME)
    assert len(initial_nuclino_data) == 3

    initial_by_ext_id = {_data_metadata(d)["external_id"]: d for d in initial_nuclino_data}
    assert set(initial_by_ext_id.keys()) == {"collection-1", "item-1", "item-2"}

    # Verify provenance and attributes in metadata
    expected_meta = {
        "collection-1": ("Collection One", "https://share.nuclino.com/c/collection-1"),
        "item-1": ("Item One", "https://share.nuclino.com/i/item-1"),
        "item-2": ("Item Two", "https://share.nuclino.com/i/item-2"),
    }
    expected_content = {
        "collection-1": "Markdown content of collection one.",
        "item-1": "Markdown content of item one.",
        "item-2": "Markdown content of item two.",
    }

    for ext_id, (expected_title, expected_url) in expected_meta.items():
        doc = initial_by_ext_id[ext_id]
        meta = _data_metadata(doc)
        assert meta["source"] == "nuclino"
        assert meta["external_id"] == ext_id
        assert meta["title"] == expected_title
        assert meta["url"] == expected_url

        # Verify stored text formatting: # {title}\n\n{content}
        text = await _read_data_text(doc)
        assert text == f"# {expected_title}\n\n{expected_content[ext_id]}"

        # Verify document-mode routing (NOT legacy relational DLT rows)
        assert not is_dlt_sourced(meta)

    # Capture initial Cognee Data IDs
    initial_ids = {ext_id: doc.id for ext_id, doc in initial_by_ext_id.items()}

    # -----------------------------------------------------------------------
    # Source scoping test: add non-Nuclino items before Run 2
    # -----------------------------------------------------------------------
    # 1. Normal standalone text document (no source tag)
    await cognee.add(
        "Standard standalone document text that is not from Nuclino.",
        dataset_name=DATASET_NAME,
    )

    # 2. Another document-source tagged DLT resource in the same dataset
    @dlt.resource(name="other_connector_items", primary_key="id", write_disposition="merge")
    def other_connector_source():
        yield {
            "id": "other-doc-1",
            "title": "Other Connector Doc",
            "content": "Content from another document connector.",
        }

    other_res = other_connector_source()
    setattr(other_res, DOCUMENT_SOURCE_ATTR, "other_connector")
    await cognee.add(
        other_res,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    all_before_run2 = await _get_all_dataset_data(DATASET_NAME)
    assert len(all_before_run2) == 5  # 3 nuclino + 1 standalone + 1 other_connector

    # -----------------------------------------------------------------------
    # Run 2: Incremental re-sync with changes
    # Upstream state changes:
    #   - collection-1: untouched
    #   - item-1: modified content and updated timestamp
    #   - item-2: vanished from upstream
    # -----------------------------------------------------------------------
    mock_session.items["item-1"] = {
        "id": "item-1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item One",
        "content": "Updated markdown content of item one.",
        "lastUpdatedAt": "2026-02-01T00:00:00.000Z",
        "url": "https://share.nuclino.com/i/item-1",
    }
    del mock_session.items["item-2"]

    await cognee.add(
        nuclino_source(
            api_key="test-api-key",
            workspace_ids=["ws-1"],
            session=mock_session,
        ),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    final_nuclino_data = await _get_nuclino_data(DATASET_NAME)
    assert len(final_nuclino_data) == 2

    final_by_ext_id = {_data_metadata(d)["external_id"]: d for d in final_nuclino_data}

    # 1. item-2 no longer exists in Cognee (Nuclino disappearance -> tombstone
    #    -> DLT hard-delete -> fresh-ID reconciliation -> Cognee orphan cleanup)
    assert "item-2" not in final_by_ext_id

    # 2. collection-1 still exists and retains the same Cognee Data ID (untouched object)
    assert final_by_ext_id["collection-1"].id == initial_ids["collection-1"]
    text_col1 = await _read_data_text(final_by_ext_id["collection-1"])
    assert text_col1 == "# Collection One\n\nMarkdown content of collection one."

    # 3. item-1 exists with changed content.
    #    In Cognee, _dlt_row_identifier incorporates content_hash, so an edited row
    #    derives a fresh Data ID while the old version is deleted via orphan cleanup.
    assert final_by_ext_id["item-1"].id != initial_ids["item-1"]
    text_item1 = await _read_data_text(final_by_ext_id["item-1"])
    assert text_item1 == "# Item One\n\nUpdated markdown content of item one."

    # 4. Exactly two Nuclino documents remain after run 2
    assert len(final_nuclino_data) == 2

    # 5 & 6. Source scoping safety: verify non-Nuclino documents in the same dataset
    # were NOT removed by Nuclino's orphan cleanup
    all_after_run2 = await _get_all_dataset_data(DATASET_NAME)
    assert len(all_after_run2) == 4  # 2 nuclino + 1 standalone + 1 other_connector

    # Standalone document still exists
    standalone_docs = [d for d in all_after_run2 if "source" not in _data_metadata(d)]
    assert len(standalone_docs) == 1

    # Other-source document still exists
    other_source_docs = [
        d for d in all_after_run2 if _data_metadata(d).get("source") == "other_connector"
    ]
    assert len(other_source_docs) == 1


@pytest.mark.asyncio
async def test_nuclino_ingestion_failure_safety_preserves_records(clean_environment):
    """Prove that an upstream failure midway aborts cleanly without false orphan deletion.

    If run 1 succeeded and stored records, and run 2 fails midway during listing/fetching:
      - cognee.add() raises
      - previously stored Cognee records remain completely intact
      - no false orphan cleanup occurs
    """
    collection_1 = {
        "id": "collection-1",
        "workspaceId": "ws-1",
        "object": "collection",
        "title": "Collection One",
        "content": "Markdown content of collection one.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
    }
    item_1 = {
        "id": "item-1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item One",
        "content": "Markdown content of item one.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
    }
    item_2 = {
        "id": "item-2",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item Two",
        "content": "Markdown content of item two.",
        "lastUpdatedAt": "2026-01-01T00:00:00.000Z",
    }

    mock_session = MockNuclinoApiSession(
        workspaces=[{"id": "ws-1"}],
        collections={"collection-1": collection_1},
        items={"item-1": item_1, "item-2": item_2},
    )

    failure_dataset_name = "nuclino_failure_safety_test"

    # Run 1: Initial successful sync
    await cognee.add(
        nuclino_source(
            api_key="test-api-key",
            workspace_ids=["ws-1"],
            session=mock_session,
        ),
        dataset_name=failure_dataset_name,
        primary_key="id",
        write_disposition="merge",
    )

    initial_data = await _get_nuclino_data(failure_dataset_name)
    assert len(initial_data) == 3
    initial_ids = {d.id for d in initial_data}

    # Run 2: Configure session to fail midway when requesting items listing
    mock_session.fail_on_url = "/v0/items"

    with pytest.raises(
        (
            RuntimeError,
            requests.exceptions.RequestException,
            PipeException,
            PipelineStepFailed,
            DLTIngestionError,
        )
    ):
        await cognee.add(
            nuclino_source(
                api_key="test-api-key",
                workspace_ids=["ws-1"],
                session=mock_session,
            ),
            dataset_name=failure_dataset_name,
            primary_key="id",
            write_disposition="merge",
        )

    # Verify that previously stored records are 100% intact and no orphans were deleted
    current_data = await _get_nuclino_data(failure_dataset_name)
    assert len(current_data) == 3
    assert {d.id for d in current_data} == initial_ids
