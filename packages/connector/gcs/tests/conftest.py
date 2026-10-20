"""Shared storage doubles and isolated SQLite pipelines for offline tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import dlt
import pytest
from cognee_community_connector_gcs import gcs_source
from google.cloud import storage


@pytest.fixture(autouse=True)
def offline_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing-key.json"))
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")

    def unexpected_access(*args, **kwargs):
        raise AssertionError(
            "Offline tests must not discover real credentials or make HTTP requests"
        )

    monkeypatch.setattr("google.auth.default", unexpected_access)
    monkeypatch.setattr("google.auth.load_credentials_from_file", unexpected_access)
    monkeypatch.setattr("requests.sessions.Session.request", unexpected_access)


@pytest.fixture
def make_blob():
    def make_blob(name="docs/a.txt", generation="123456", metageneration="1"):
        return SimpleNamespace(
            name=name,
            size=5,
            generation=generation,
            metageneration=metageneration,
            updated=datetime(2026, 10, 20, 10, tzinfo=UTC),
            download_as_bytes=MagicMock(return_value=b"hello"),
        )

    return make_blob


@pytest.fixture
def bucket():
    result = create_autospec(storage.Bucket, instance=True, spec_set=True)
    result.get_blob.return_value = None
    return result


@pytest.fixture
def client(bucket):
    result = create_autospec(storage.Client, instance=True, spec_set=True)
    result.bucket.return_value = bucket
    bucket.blob.side_effect = lambda name: next(
        blob for blob in result.list_blobs.return_value if blob.name == name
    )
    return result


@pytest.fixture
def pipeline(tmp_path):
    def make_pipeline(dataset="documents"):
        return dlt.pipeline(
            pipeline_name=f"gcs_state_test_{dataset}",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'documents.db'}"),
            dataset_name=dataset,
            pipelines_dir=str(tmp_path / "state"),
        )

    return make_pipeline


@pytest.fixture
def run_sync(client):
    def sync(pipeline, write_disposition="merge", **kwargs):
        pipeline.run(
            gcs_source("company-documents", "docs/", client=client, **kwargs),
            write_disposition=write_disposition,
        )

    return sync


@pytest.fixture
def rows():
    def read_rows(pipeline):
        with pipeline.sql_client() as sql_client:
            with sql_client.execute_query("SELECT id, content FROM gcs_documents") as cursor:
                return dict(cursor.fetchall())

    return read_rows


@pytest.fixture
def read_inventories():
    def read_inventories(pipeline):
        return pipeline.state["sources"]["gcs"]["resources"]["gcs_documents"]["inventories"]

    return read_inventories
