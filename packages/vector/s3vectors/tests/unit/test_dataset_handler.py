"""Offline tests for the S3 Vectors dataset database handler (multi-user mode)."""

from types import SimpleNamespace
from uuid import uuid4

import cognee_community_vector_adapter_s3vectors.S3VectorsDatasetDatabaseHandler as handler_module
import pytest
from cognee_community_vector_adapter_s3vectors.S3VectorsDatasetDatabaseHandler import (
    S3VectorsDatasetDatabaseHandler,
)


def make_config(**overrides):
    config = SimpleNamespace(
        vector_db_provider="s3vectors",
        vector_db_url="",
        vector_db_name="cognee",
        vector_db_username="akid",
        vector_db_password="secret",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def make_dataset_database(**overrides):
    fields = {
        "vector_database_provider": "s3vectors",
        "vector_database_url": "",
        "vector_database_name": "some-dataset",
        "vector_database_connection_info": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeVectorEngine:
    def __init__(self):
        self.pruned = False

    async def prune(self):
        self.pruned = True


async def test_create_dataset_names_a_bucket_after_the_dataset(monkeypatch):
    monkeypatch.setattr(handler_module, "get_vectordb_config", make_config)
    dataset_id = uuid4()

    info = await S3VectorsDatasetDatabaseHandler.create_dataset(dataset_id, None)

    assert info["vector_database_provider"] == "s3vectors"
    assert info["vector_database_name"] == str(dataset_id)
    assert info["vector_dataset_database_handler"] == "s3vectors"
    # Credentials are never persisted with the dataset.
    assert "vector_database_key" not in info


async def test_create_dataset_drops_non_url_endpoint_values(monkeypatch):
    monkeypatch.setattr(
        handler_module,
        "get_vectordb_config",
        lambda: make_config(vector_db_url="C:\\cognee\\databases\\cognee.lancedb"),
    )

    info = await S3VectorsDatasetDatabaseHandler.create_dataset(uuid4(), None)
    assert info["vector_database_url"] == ""

    endpoint = "https://vpce-0abc.s3vectors.us-east-1.vpce.amazonaws.com"
    monkeypatch.setattr(
        handler_module, "get_vectordb_config", lambda: make_config(vector_db_url=endpoint)
    )

    info = await S3VectorsDatasetDatabaseHandler.create_dataset(uuid4(), None)
    assert info["vector_database_url"] == endpoint


async def test_create_dataset_rejects_a_mismatched_provider(monkeypatch):
    monkeypatch.setattr(
        handler_module, "get_vectordb_config", lambda: make_config(vector_db_provider="lancedb")
    )

    with pytest.raises(ValueError):
        await S3VectorsDatasetDatabaseHandler.create_dataset(uuid4(), None)


async def test_resolve_dataset_connection_info_injects_config_credentials(monkeypatch):
    monkeypatch.setattr(handler_module, "get_vectordb_config", make_config)
    dataset_database = make_dataset_database()

    resolved = await S3VectorsDatasetDatabaseHandler.resolve_dataset_connection_info(
        dataset_database
    )

    assert resolved.vector_database_connection_info["username"] == "akid"
    assert resolved.vector_database_connection_info["password"] == "secret"


async def test_delete_dataset_prunes_the_dataset_engine(monkeypatch):
    monkeypatch.setattr(handler_module, "get_vectordb_config", make_config)
    engine = FakeVectorEngine()
    engine_kwargs = {}

    def fake_create_vector_engine(**kwargs):
        engine_kwargs.update(kwargs)
        return engine

    monkeypatch.setattr(handler_module, "create_vector_engine", fake_create_vector_engine)

    await S3VectorsDatasetDatabaseHandler.delete_dataset(make_dataset_database())

    assert engine.pruned is True
    assert engine_kwargs["vector_db_provider"] == "s3vectors"
    assert engine_kwargs["vector_db_name"] == "some-dataset"
    assert engine_kwargs["vector_db_username"] == "akid"
    assert engine_kwargs["vector_db_password"] == "secret"
