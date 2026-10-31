"""CI-safe smoke test for the packaged ``examples/example.py``.

Runs the example's ``main()`` end-to-end with the Egnyte API, the LLM and the
embeddings mocked, so no credentials are needed.  It checks the example still
matches the connector's public API and env-var configuration.
"""

import importlib.util
import pathlib

import cognee
import pytest
import pytest_asyncio
from _fake_egnyte import FakeEgnyte
from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
    create_embedding_engine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_egnyte import egnyte as eg

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example_module():
    spec = importlib.util.spec_from_file_location("egnyte_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        return KnowledgeGraph(
            nodes=[KGNode(id="Acme", name="Acme", type="Company", description="A company")],
            edges=[],
        )
    return response_model()


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("EGNYTE_DOMAIN", "acme")
    monkeypatch.setenv("EGNYTE_FOLDER_PATHS", "/Shared/Projects")
    monkeypatch.setenv("EGNYTE_ACCESS_TOKEN", "unused-token")

    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    # Vector indexing stays on (with the mock embeddings below) so that
    # recall() in the example has a searchable index, as in a real run.
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    # cognee's own mock embeddings (zero vectors).  The engine is cached, so
    # clear it to make sure this test's setting is the one in use.
    monkeypatch.setenv("MOCK_EMBEDDING", "true")
    create_embedding_engine.cache_clear()

    fake = FakeEgnyte()
    fake.put("/Shared/Projects/acme.txt", "Acme is a company based in Springfield.")
    built_with = []

    def fake_build_client(domain, *args, **kwargs):
        built_with.append(domain)
        return fake

    monkeypatch.setattr(eg, "_build_client", fake_build_client)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield built_with
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    create_embedding_engine.cache_clear()


@pytest.mark.asyncio
async def test_example_runs_without_live_credentials(clean_environment):
    pytest.importorskip("ladybug")

    await _load_example_module().main()

    # The example configured itself from EGNYTE_DOMAIN.
    assert clean_environment and set(clean_environment) == {"acme"}
