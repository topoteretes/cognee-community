import importlib
from types import SimpleNamespace

import boto3
import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from moto import mock_aws

from cognee_community_connector_s3 import s3_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")
resolve_dirs_module = importlib.import_module("cognee.tasks.ingestion.resolve_data_directories")

DATASET = "s3_connector_integration"
BUCKET = "cognee-s3-integration"
PREFIX = "docs/"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        any(token in str(value).lower() for value in (props or {}).values())
        for _node_id, props in nodes
    )


async def _s3_data(dataset_name: str):
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[dataset_name]
    )
    if not datasets:
        return []
    records = await get_dataset_data(datasets[0].id)
    return [
        record
        for record in records
        if isinstance(record.system_metadata, dict) and record.system_metadata.get("source") == "s3"
    ]


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))

    # Moto injects fake AWS environment credentials. Cognee's generic path
    # resolver must not mistake those for a request to use its own s3fs loader;
    # this test exercises the connector's explicit boto3 client instead.
    monkeypatch.setattr(
        resolve_dirs_module,
        "get_s3_config",
        lambda: SimpleNamespace(aws_access_key_id=None, aws_secret_access_key=None),
    )

    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination

    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    get_dlt_destination.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    Container()[PipelineContext].deactivate()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _add(client, dataset=DATASET):
    await cognee.add(
        s3_source(BUCKET, PREFIX, client=client),
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


@pytest.mark.asyncio
async def test_cognee_incremental_sync_and_final_delete(clean_environment):
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key="docs/a.txt", Body=b"version one")
        client.put_object(Bucket=BUCKET, Key="docs/b.txt", Body=b"keep")

        await _add(client)
        first = await _s3_data(DATASET)
        assert {record.system_metadata["external_id"] for record in first} == {
            "s3://cognee-s3-integration/docs/a.txt",
            "s3://cognee-s3-integration/docs/b.txt",
        }

        client.put_object(Bucket=BUCKET, Key="docs/a.txt", Body=b"version two")
        client.delete_object(Bucket=BUCKET, Key="docs/b.txt")
        await _add(client)
        second = await _s3_data(DATASET)
        assert [record.system_metadata["external_id"] for record in second] == [
            "s3://cognee-s3-integration/docs/a.txt"
        ]

        client.delete_object(Bucket=BUCKET, Key="docs/a.txt")
        await _add(client)
        assert await _s3_data(DATASET) == []


@pytest.mark.asyncio
async def test_deletions_remove_cognified_graph_content(clean_environment):
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        client.put_object(
            Bucket=BUCKET,
            Key="docs/a.txt",
            Body=f"{ALPHA} is a logistics company.".encode(),
        )
        client.put_object(
            Bucket=BUCKET,
            Key="docs/b.txt",
            Body=f"{BRAVO} is a finance company.".encode(),
        )

        await _add(client)
        await cognee.cognify(datasets=[DATASET])
        assert await _graph_has(ALPHA)
        assert await _graph_has(BRAVO)

        client.delete_object(Bucket=BUCKET, Key="docs/b.txt")
        await _add(client)
        await cognee.cognify(datasets=[DATASET])
        assert await _graph_has(ALPHA)
        assert not await _graph_has(BRAVO)

        client.delete_object(Bucket=BUCKET, Key="docs/a.txt")
        await _add(client)
        assert not await _graph_has(ALPHA)


@pytest.mark.asyncio
async def test_two_prefixes_in_one_dataset_are_cleanup_isolated(clean_environment):
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key="alpha/a.txt", Body=b"alpha")
        client.put_object(Bucket=BUCKET, Key="beta/b.txt", Body=b"beta")

        for prefix in ("alpha/", "beta/"):
            await cognee.add(
                s3_source(BUCKET, prefix, client=client),
                dataset_name=DATASET,
                primary_key="id",
                write_disposition="merge",
                max_rows_per_table=0,
            )

        records = await _s3_data(DATASET)
        assert {record.system_metadata["external_id"] for record in records} == {
            "s3://cognee-s3-integration/alpha/a.txt",
            "s3://cognee-s3-integration/beta/b.txt",
        }

        client.delete_object(Bucket=BUCKET, Key="alpha/a.txt")
        await cognee.add(
            s3_source(BUCKET, "alpha/", client=client),
            dataset_name=DATASET,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )

        records = await _s3_data(DATASET)
        assert [record.system_metadata["external_id"] for record in records] == [
            "s3://cognee-s3-integration/beta/b.txt"
        ]