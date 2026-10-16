"""Ingestion suite for Semrush connector testing integration with cognee's dlt resolver.

Follows the real resolve_dlt_sources and ingest_dlt_source pipeline path with only
HTTP transport mocked (no mocking of the resolver, dlt pipeline, or sqlite staging engine).
"""

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
import requests
from cognee.infrastructure.databases.relational import create_db_and_tables
from cognee.infrastructure.databases.relational.config import get_relational_config
from cognee.modules.users.models import User
from cognee.tasks.ingestion.dlt_utils import (
    PIPELINE_SCOPE_ATTR,
    document_source_tag,
    pipeline_name_for_source,
)
from cognee.tasks.ingestion.resolve_dlt_sources import resolve_dlt_sources

from cognee_community_connector_semrush.semrush import (
    SEMRUSH_SOURCE_NAME,
    semrush_source,
)

_DOC_RANKS_HEADER = (
    "Database;Domain;Rank;Organic Keywords;Organic Traffic;Organic Cost;"
    "Adwords Keywords;Adwords Traffic;Adwords Cost"
)
_DOC_ORGANIC_HEADER = (
    "Keyword;Position;Previous Position;Position Difference;Search Volume;CPC;"
    "Url;Traffic (%);Traffic Cost (%);Competition;Number of Results;Trends"
)
_RANKS_SAMPLE = f"{_DOC_RANKS_HEADER}\nus;example.com;150;5000;12000;3450.50;10;150;25.75\n"
_ORGANIC_SAMPLE = (
    f"{_DOC_ORGANIC_HEADER}\n"
    "seo tool;1;2;-1;1000;2.50;https://example.com/tool;15.2;5.1;0.85;120000;0.5\n"
)


def _mock_semrush_http_transport(ranks_data: str, organic_data: str):
    """Transport-level mock for requests.Session.send."""

    def _send(*args, **kwargs):
        request = args[1] if len(args) > 1 else args[0]
        url = request.url or ""
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp.request = request

        if "type=domain_ranks" in url:
            resp._content = ranks_data.encode("utf-8")
        elif "type=domain_organic" in url:
            resp._content = organic_data.encode("utf-8")
        else:
            resp._content = b"ERROR 50 :: NOTHING FOUND"
        return resp

    return _send


@pytest.fixture
def isolated_cognee_env(tmp_path, monkeypatch):
    """Isolate cognee relational staging DB and dlt pipeline state inside tmp_path."""
    staging_dir = tmp_path / "cognee_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    dlt_dir = tmp_path / "dlt_pipelines"
    dlt_dir.mkdir(parents=True, exist_ok=True)

    rel_config = get_relational_config()
    monkeypatch.setattr(rel_config, "db_path", str(staging_dir))
    monkeypatch.setenv("DLT_DATA_DIR", str(dlt_dir))

    return {"staging_dir": staging_dir, "dlt_dir": dlt_dir}


@pytest.mark.anyio
async def test_semrush_relational_contract() -> None:
    """Verify Semrush connector satisfies the cognee relational manifest contract."""
    source = semrush_source(
        api_key="test_key",
        domains=["example.com"],
        display_date="20260915",
    )

    # 1. DOCUMENT_SOURCE_ATTR must be unset so resolve_dlt_sources routes to
    # the relational manifest path
    assert document_source_tag(source) is None

    # 2. Pipeline scope must be 'semrush' to generate deterministic pipeline names across runs
    assert getattr(source, PIPELINE_SCOPE_ATTR, None) == SEMRUSH_SOURCE_NAME
    p_name = pipeline_name_for_source(source, "default_dataset")
    assert p_name.startswith("ingest_dlt_")


@pytest.mark.anyio
async def test_semrush_ingestion_manifest_and_period_transition(isolated_cognee_env) -> None:
    """Test full resolve_dlt_sources flow: initial ingest, manifest structure, and period update."""
    await create_db_and_tables()

    user = User(id=uuid4(), email="tester@example.com")
    dataset_name = "semrush_test_ds"
    mock_send = _mock_semrush_http_transport(_RANKS_SAMPLE, _ORGANIC_SAMPLE)

    with patch.object(requests.Session, "send", side_effect=mock_send):
        # -------------------------------------------------------------------
        # Run 1: Ingestion of period 20260915
        # -------------------------------------------------------------------
        source1 = semrush_source(
            api_key="test_key",
            domains=["example.com"],
            display_date="20260915",
        )
        items1, cleanup1 = await resolve_dlt_sources(
            [source1],
            dataset_name=dataset_name,
            user=user,
            write_disposition="replace",
        )

        assert len(items1) == 1
        manifest_item1 = items1[0]

        # Verify metadata
        assert manifest_item1.system_metadata["source"] == "dlt_source"
        assert manifest_item1.system_metadata["source_name"] == SEMRUSH_SOURCE_NAME
        assert set(manifest_item1.system_metadata["tables"]) == {"domain_ranks", "domain_organic"}
        assert manifest_item1.system_metadata["row_count"] == 2
        assert cleanup1 is not None

        # Verify JSON manifest content
        manifest1 = json.loads(manifest_item1.data)
        assert manifest1["version"] == 1
        assert manifest1["source_name"] == SEMRUSH_SOURCE_NAME
        assert set(manifest1["tables"].keys()) == {"domain_ranks", "domain_organic"}
        assert len(manifest1["rows"]) == 2

        # Check row snapshots and content hashes
        rank_row1 = next(r for r in manifest1["rows"] if r["table_name"] == "domain_ranks")
        assert "2026-09-15" in rank_row1["text"]
        hash1 = rank_row1["content_hash"]
        data_id1 = manifest_item1.data_id

        # -------------------------------------------------------------------
        # Run 2: Transition to new period 20261015
        # -------------------------------------------------------------------
        source2 = semrush_source(
            api_key="test_key",
            domains=["example.com"],
            display_date="20261015",
        )
        items2, _cleanup2 = await resolve_dlt_sources(
            [source2],
            dataset_name=dataset_name,
            user=user,
            write_disposition="replace",
        )

        assert len(items2) == 1
        manifest_item2 = items2[0]

        # In cognee, the manifest document DataItem ID is stable across periods
        # (seeded from dlt_manifest_identifier(dataset_name, source_name)),
        # which allows cognee to update the manifest Data record in-place.
        assert manifest_item2.data_id == data_id1

        # Manifest text and content hashes differ across periods because snapshot_date changed
        assert manifest_item2.data != manifest_item1.data
        manifest2 = json.loads(manifest_item2.data)
        rank_row2 = next(r for r in manifest2["rows"] if r["table_name"] == "domain_ranks")
        assert "2026-10-15" in rank_row2["text"]
        hash2 = rank_row2["content_hash"]
        assert hash2 != hash1


@pytest.mark.anyio
async def test_skipped_run_behavior_on_cognee_path(isolated_cognee_env) -> None:
    """Verify cognee behavior when the skip gate deselects resources.

    =============================================================================
    ITEM 6: SKIPPED-RUN BEHAVIOR ON THE COGNEE PATH (EVIDENCE & DOCUMENTATION):
    =============================================================================
    When the selection fingerprint and period match state['cursor_*'], the
    semrush connector sets `selected = False` on both domain_ranks and domain_organic.
    As a result:
      1. dlt executes 0 extract/normalize jobs and produces 0 load packages.
      2. In cognee's `ingest_dlt_source.py`, `loaded_tables` is constructed strictly
         by inspecting completed jobs in `load_info.load_packages`:
             for package in load_info.load_packages:
                 for job in package.jobs.get("completed_jobs", []):
                     loaded_tables.add(job.job_file_info.table_name)
         Because there are zero load packages, `loaded_tables` evaluates to `set()`.
      3. `_extract_dlt_schema` filters the staging database schema to ONLY tables in
         `loaded_tables` (`allowed_tables`). Since `loaded_tables` is empty,
         `filtered_schema` is `{}`.
      4. `_read_rows_from_tables` iterates over `filtered_schema.items()`, reading
         ZERO rows (`[]`). It does NOT read any rows from the destination database.
      5. `_build_source_manifest_item` receives `rows=[]` and returns `None`.
      6. `resolve_dlt_sources` appends nothing to `expanded_items`, and
         `manifest_data_ids` is empty.
      7. `do_manifest_cleanup` evaluates to `False`, returning `orphan_cleanup = None`.
      8. `resolve_dlt_sources` returns `([], None)`.
    Conclusion: Cognee reads ONLY `load_info` (which indicates no load jobs), skips
    reading the destination dataset entirely, produces 0 DataItems, schedules 0
    orphan cleanups, and leaves the existing graph/vector store completely unchanged.
    =============================================================================
    """
    await create_db_and_tables()

    user = User(id=uuid4(), email="tester@example.com")
    dataset_name = "semrush_skip_ds"
    mock_send = _mock_semrush_http_transport(_RANKS_SAMPLE, _ORGANIC_SAMPLE)

    with patch.object(requests.Session, "send", side_effect=mock_send):
        # Initial run: populates staging and commits cursor
        s1 = semrush_source(
            api_key="test_key",
            domains=["example.com"],
            display_date="20260915",
        )
        items1, cleanup1 = await resolve_dlt_sources(
            [s1],
            dataset_name=dataset_name,
            user=user,
            write_disposition="replace",
        )
        assert len(items1) == 1
        assert cleanup1 is not None

        # Second run: identical parameters triggers skip gate
        s2 = semrush_source(
            api_key="test_key",
            domains=["example.com"],
            display_date="20260915",
        )
        items2, cleanup2 = await resolve_dlt_sources(
            [s2],
            dataset_name=dataset_name,
            user=user,
            write_disposition="replace",
        )

        # Assert cognee produces 0 manifest items and 0 orphan cleanups
        assert items2 == []
        assert cleanup2 is None
