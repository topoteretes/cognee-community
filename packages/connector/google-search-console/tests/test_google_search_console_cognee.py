"""End-to-end: Search Console changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify -> orphan cleanup)
against the fake Search Console API, with a mocked LLM and mocked embeddings, so no network,
Google account or API key is needed. Each property's queries carry one distinctive company name,
which the mocked LLM turns into exactly one graph entity.
"""

import importlib
from datetime import date

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
from fake_search_console import ACCESS_TOKEN, FakeSearchConsole

from cognee_community_connector_google_search_console import (
    SearchConsoleClient,
    SearchConsoleCredentials,
    google_search_console_source,
)
from cognee_community_connector_google_search_console.google_search_console import _page_id

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "search_console_forget_test"
SITE = "sc-domain:example.com"
BLOG = "https://blog.example.com/"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
DELTA = "Deltacorp"
ECHO = "Echocorp"
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA, ECHO)

# Chunks the mocked LLM extracted a graph from, in call order.
EXTRACTED: list[str] = []


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    when the configured LLM provider is Gemini, so the mock must work with
    both variants.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        EXTRACTED.append(text_input or "")
        name = next((t for t in TOKENS if text_input and t in text_input), None)
        nodes = (
            [
                _with_required(
                    KGNode, id=name, name=name, type="Company", description=f"{name} entity"
                )
            ]
            if name
            else []
        )
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # dlt keeps pipeline state (the cursors) outside cognee's directories.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    # The dlt staging destination is cached per process; without this a test
    # would keep loading into the previous test's (deleted) system directory.
    get_dlt_destination.cache_clear()

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    EXTRACTED.clear()
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


@pytest.fixture
def fake(today):
    fake = FakeSearchConsole(today)
    fake.add_site(SITE)
    fake.add_site(BLOG)
    fake.add_traffic(SITE, date(2026, 9, 10), f"{ALPHA} pricing", "https://example.com/a", clicks=9)
    fake.add_traffic(SITE, date(2026, 10, 5), f"{BRAVO} login", "https://example.com/b", clicks=8)
    fake.add_traffic(BLOG, date(2026, 10, 5), f"{CHARLIE} review", f"{BLOG}post", clicks=7)
    return fake


async def _sync(fake, today, **options):
    client = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=fake.transport(),
        sleep=lambda _s: None,
    )
    await cognee.add(
        google_search_console_source(client=client, today=today, months=2, **options),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


async def _documents() -> dict[str, dict]:
    from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    (dataset,) = await get_datasets_by_name(DATASET, user.id)
    return {
        data.system_metadata.get("external_id"): data.system_metadata
        for data in await get_dataset_data(dataset.id)
    }


@pytest.mark.asyncio
async def test_performance_is_ingested_and_a_deselected_property_is_forgotten(
    clean_environment, fake, today
):
    await _sync(fake, today)

    for token in (ALPHA, BRAVO, CHARLIE):
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    documents = await _documents()
    overview = documents[f"google_search_console:overview:{SITE}:2026-10"]
    assert overview["source"] == "google_search_console"

    # The blog is taken out of the selection.
    await _sync(fake, today, sites=[SITE])

    assert not await _graph_has(CHARLIE), "a deselected property must be forgotten"
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO), "the other property must stay"
    assert f"google_search_console:overview:{BLOG}:2026-10" not in await _documents()


@pytest.mark.asyncio
async def test_a_property_removed_from_the_account_is_forgotten(clean_environment, fake, today):
    await _sync(fake, today)
    assert await _graph_has(CHARLIE)

    fake.remove_site(BLOG)
    await _sync(fake, today)

    assert not await _graph_has(CHARLIE)
    assert await _graph_has(ALPHA)


@pytest.mark.asyncio
async def test_a_page_leaving_the_top_pages_loses_its_document(clean_environment, fake, today):
    bravo_page = _page_id("google_search_console", SITE, "2026-10", "https://example.com/b")
    await _sync(fake, today, sites=[SITE], top_pages=1)
    assert bravo_page in await _documents()

    fake.add_traffic(SITE, date(2026, 10, 6), f"{DELTA} deal", "https://example.com/d", clicks=50)
    await _sync(fake, today, sites=[SITE], top_pages=1)

    documents = await _documents()
    assert bravo_page not in documents, "the page fell out of the month's top pages"
    assert f"google_search_console:overview:{SITE}:2026-10" in documents
    assert await _graph_has(DELTA), "the new top page is ingested"


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_call_the_llm_again(clean_environment, fake, today):
    await _sync(fake, today)
    assert EXTRACTED, "the first sync should extract a graph"

    EXTRACTED.clear()
    await _sync(fake, today)
    assert EXTRACTED == [], "unchanged months must not be cognified again"

    # Traffic in the open month changes its overview and that page only.
    fake.add_traffic(SITE, date(2026, 10, 14), f"{ECHO} news", "https://example.com/b", clicks=5)
    await _sync(fake, today)
    assert EXTRACTED, "a changed month is cognified"
    assert all("October 2026" in chunk for chunk in EXTRACTED), "closed months are not touched"
    assert any(ECHO in chunk for chunk in EXTRACTED)
