"""End-to-end: Dropbox changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) with
the in-memory Dropbox, a mocked LLM and mocked embeddings, so no network or
API key is needed.  Each file carries one distinctive company name, which the
mocked LLM turns into exactly one graph entity.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
from cognee.modules.users.methods import get_default_user
from fake_dropbox import FakeDropbox

from cognee_community_connector_dropbox import dropbox_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

try:
    # Newer cognee returns vectors plus a token count; 1.6.3 a plain list.
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

DATASET = "dropbox_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    (label, summary, description) when the configured LLM provider is Gemini,
    so the mock must work with both variants.
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


@pytest.fixture(scope="session")
def cognee_root(tmp_path_factory):
    # One root for the whole session: cognee caches its database engines per
    # process, so a new root per test would leave later tests reading the
    # first test's stores.  prune() below empties it between tests instead.
    return tmp_path_factory.mktemp("cognee")


@pytest_asyncio.fixture
async def clean_environment(cognee_root, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # dlt keeps pipeline state under ~/.dlt, which cognee's prune never clears,
    # so without this a second test run would start from the first run's cursors.
    # dlt also restores state from its destination, so each test below syncs
    # under its own resource_name.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(cognee_root / "data"))
    cognee.config.system_root_directory(str(cognee_root / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        return EmbeddingResult(vectors, tokens=0) if EmbeddingResult else vectors

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _stored_documents() -> int:
    """How many data items the dataset holds.

    A tombstone that only empties a row (hard_delete off) leaves an empty
    document behind, which the graph checks alone would not notice.
    """
    user = await get_default_user()
    datasets = await get_datasets_by_name([DATASET], user.id)
    return len(await get_dataset_data(datasets[0].id))


async def _sync(fake, resource_name="dropbox_files"):
    await cognee.add(
        dropbox_source(service=fake, resource_name=resource_name),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_file_and_folder_are_forgotten_moved_file_is_kept(clean_environment):
    fake = FakeDropbox()
    fake.put("/Notes/alpha.md", f"{ALPHA} is a company in the logistics sector.")
    fake.put("/Notes/bravo.txt", f"{BRAVO} is an unrelated company in finance.")
    fake.put("/Old/Deep/charlie.md", f"{CHARLIE} builds rockets.")

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    assert await _stored_documents() == 3

    # Move alpha, delete bravo, delete the whole /Old folder.
    fake.move("/Notes/alpha.md", "/Archive/alpha.md")
    fake.delete("/Notes/bravo.txt")
    fake.delete("/Old")
    await _sync(fake)

    assert await _graph_has(ALPHA), "a moved file must stay in memory"
    assert not await _graph_has(BRAVO), "a deleted file must be forgotten from the graph"
    assert not await _graph_has(CHARLIE), "files in a deleted folder must be forgotten"
    assert await _stored_documents() == 1, "deleted files must be removed, not left empty"


@pytest.mark.asyncio
async def test_two_dropbox_sources_in_one_dataset_stay_apart(clean_environment):
    work, personal = FakeDropbox(), FakeDropbox()
    work.put("/Notes/alpha.md", f"{ALPHA} is a company in the logistics sector.")
    personal.put("/Notes/bravo.md", f"{BRAVO} is an unrelated company in finance.")

    # Each sync of one account must leave the other account's files alone.
    await _sync(work, "work_dropbox")
    await _sync(personal, "personal_dropbox")
    await _sync(work, "work_dropbox")
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    assert await _stored_documents() == 2

    work.delete("/Notes/alpha.md")
    await _sync(work, "work_dropbox")

    assert not await _graph_has(ALPHA)
    assert await _graph_has(BRAVO), "a delete in one account must not touch the other"
    assert await _stored_documents() == 1


@pytest.mark.asyncio
async def test_overwritten_and_emptied_files_leave_memory(clean_environment):
    fake = FakeDropbox()
    fake.put("/Notes/alpha.md", f"{ALPHA} is a company in the logistics sector.")
    fake.put("/Notes/bravo.md", f"{BRAVO} is an unrelated company in finance.")
    await _sync(fake, "dropbox_overwrites")
    assert await _stored_documents() == 2

    # Delete and re-create between two cursors: the delta holds only the new file.
    fake.vanish("/Notes/alpha.md")
    fake.put("/Notes/alpha.md", f"{CHARLIE} builds rockets.")
    fake.put("/Notes/bravo.md", "   ")  # emptied, so skipped from now on
    await _sync(fake, "dropbox_overwrites")

    assert not await _graph_has(ALPHA), "the overwritten file's old version must go"
    assert await _graph_has(CHARLIE)
    assert not await _graph_has(BRAVO), "an emptied file's old text must go"
    assert await _stored_documents() == 1
