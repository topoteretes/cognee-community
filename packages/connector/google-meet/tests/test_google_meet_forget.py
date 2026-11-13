"""End-to-end: Google Meet changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify)
against the fake Meet and Drive APIs, with a mocked LLM and mocked embeddings,
so no network or API key is needed.  Each row carries one distinctive company
name, which the mocked LLM turns into exactly one graph entity.
"""

import importlib
from datetime import datetime, timedelta

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
from fake_meet import FakeDrive, FakeMeet

from cognee_community_connector_google_meet import MeetServices, google_meet_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

try:
    # Newer cognee returns vectors plus a token count; 1.6.3 a plain list.
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:
    EmbeddingResult = None

DATASET = "google_meet_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
TOKENS = (ALPHA, BRAVO, CHARLIE)
START = datetime(2026, 10, 17, 10, 0)


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
    # so without this a second test run would start from the first run's cursor.
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


async def _sync(meet, drive, resource_name="google_meet"):
    await cognee.add(
        google_meet_source(
            services=MeetServices(meet=meet, drive=drive), resource_name=resource_name
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_transcript_doc_is_forgotten_and_the_attendance_stays(clean_environment):
    drive = FakeDrive()
    meet = FakeMeet(drive=drive)
    conference = meet.start_conference(meet.add_space(), START)
    # The guest's name is the conference's token; the speaker's name is not.
    speaker = meet.add_participant(conference, "Meera Joshi", [(START, START + timedelta(hours=1))])
    meet.add_participant(
        conference, ALPHA, [(START, START + timedelta(hours=1))], kind="anonymousUser"
    )
    meet.end_conference(conference, START + timedelta(hours=1))
    meet.add_transcript(conference, [(speaker, f"{BRAVO} builds the drone.", START)])
    meet.add_transcript(
        conference, [(speaker, f"{CHARLIE} pays for it.", START)], doc_id="doc-budget"
    )

    await _sync(meet, drive)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    assert await _stored_documents() == 3  # the conference and two transcripts

    drive.trash("doc-budget")
    await _sync(meet, drive)

    assert await _graph_has(ALPHA), "the attendance record must stay"
    assert await _graph_has(BRAVO), "an untouched transcript must stay"
    assert not await _graph_has(CHARLIE), "a transcript whose Doc was deleted must be forgotten"
    assert await _stored_documents() == 2, "a deleted transcript must be removed, not left empty"


@pytest.mark.asyncio
async def test_two_google_accounts_in_one_dataset_stay_apart(clean_environment):
    work_drive, personal_drive = FakeDrive(), FakeDrive()
    work, personal = FakeMeet(drive=work_drive), FakeMeet(drive=personal_drive)
    for meet, guest, doc_id in ((work, ALPHA, "doc-work"), (personal, BRAVO, "doc-home")):
        conference = meet.start_conference(meet.add_space(), START)
        speaker = meet.add_participant(
            conference, "Meera Joshi", [(START, START + timedelta(hours=1))]
        )
        meet.end_conference(conference, START + timedelta(hours=1))
        meet.add_transcript(conference, [(speaker, f"{guest} joined.", START)], doc_id=doc_id)

    # Each sync of one account must leave the other account's meetings alone.
    await _sync(work, work_drive, "work_meet")
    await _sync(personal, personal_drive, "personal_meet")
    await _sync(work, work_drive, "work_meet")
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    assert await _stored_documents() == 4

    work_drive.trash("doc-work")
    await _sync(work, work_drive, "work_meet")

    assert not await _graph_has(ALPHA)
    assert await _graph_has(BRAVO), "a delete in one account must not touch the other"
    assert await _stored_documents() == 3
