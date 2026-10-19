"""End-to-end: the Figma connector through the real ``cognee.add`` + ``cognify``.

Figma is served by ``FakeFigma`` and the LLM and embeddings are mocked, so no
Figma account or API key is needed. Verifies the acceptance criteria at the
memory layer:

* files and comment threads are ingested as documents (``source="figma"``)
  and cognified;
* a re-sync keeps an unchanged file's data_id stable without fetching its
  node tree again, so it is neither re-fetched nor re-ingested, and makes no
  LLM call; only changed text is cognified again;
* deleting a file or a comment upstream removes its extracted entity from
  the graph on the next sync (forget-on-delete), while the rest survives;
* the OAuth integration lists folders for the picker, syncs the stored
  selection with the stored token, refreshes an expiring token, and forgets
  everything once the selection is emptied;
* the packaged ``examples/example.py`` runs end to end.
"""

import base64
import importlib
import importlib.util
import os
import pathlib
from datetime import UTC, datetime, timedelta

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
from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
from fake_figma import TOKEN, FakeFigma, frame, text

from cognee_community_connector_figma import figma_source, integration
from cognee_community_connector_figma.integration import FigmaIntegration

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"

DATASET = "figma_e2e_test"
TEAM = "team-1"
DESIGN = "folder-design"
# Distinctive tokens so each document maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"


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
        names = [t for t in (ALPHA, BRAVO, CHARLIE) if text_input and t in text_input]
        nodes = [
            _with_required(KGNode, id=n, name=n, type="Company", description=f"{n} entity")
            for n in names
        ]
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _node_id, props in nodes for value in (props or {}).values()
    )


async def _dataset_documents(dataset: str = DATASET) -> dict:
    """Return {external_id: data_id} for the Data records in a dataset."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets([dataset], "read", user)
    if not datasets:
        return {}
    records = await get_dataset_data(datasets[0].id)
    return {(record.system_metadata or {}).get("external_id"): record.id for record in records}


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # cognee's ingest pipeline keeps dlt state (the render cache) in the
    # default pipelines dir; isolate it so a previous run can't leak into this.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", base64.b64encode(os.urandom(32)).decode())
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


def _fake_figma() -> FakeFigma:
    figma = FakeFigma()
    figma.add_team(TEAM, "Acme")
    figma.add_folder(TEAM, DESIGN, "Design")
    figma.add_file(
        DESIGN,
        "file-a",
        "Logistics portal",
        {"Flows": [frame("Contract", text(f"{ALPHA} signed the logistics contract."))]},
    )
    figma.add_file(
        DESIGN,
        "file-b",
        "Finance prospect",
        {"Pitch": [frame("Intro", text(f"{BRAVO} is an unrelated finance prospect."))]},
    )
    return figma


async def _sync(figma: FakeFigma) -> None:
    await cognee.add(
        figma_source(TOKEN, folder_ids=[DESIGN], session=figma.session()),
        dataset_name=DATASET,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_files_and_comments_are_remembered_and_deletions_forgotten(clean_environment):
    figma = _fake_figma()
    root = figma.comment("file-a", f"Should {CHARLIE} co-brand the contract screen?")
    figma.comment("file-a", "Yes, add their logo.", author="Bob", parent=root)

    await _sync(figma)
    documents = await _dataset_documents()
    thread_id = f"comment:file-a:{root}"
    assert set(documents) == {"file:file-a", "file:file-b", thread_id}
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    assert await _graph_has(CHARLIE)

    # Delete Bravo's file and the comment thread upstream; the next sync must
    # forget both without re-fetching the unchanged file.
    figma.delete_file("file-b")
    figma.delete_comment("file-a", root)
    figma.requests.clear()
    await _sync(figma)

    after = await _dataset_documents()
    assert set(after) == {"file:file-a"}
    assert figma.file_requests() == [], "an unchanged file must come from the cache"
    # The unchanged file kept its content-hash data_id (not re-ingested).
    assert after["file:file-a"] == documents["file:file-a"]
    assert await _graph_has(ALPHA), "surviving file's entity must remain"
    assert not await _graph_has(BRAVO), "deleted file's entity must be forgotten"
    assert not await _graph_has(CHARLIE), "deleted comment's entity must be forgotten"


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_call_the_llm_again(clean_environment):
    figma = _fake_figma()
    root = figma.comment("file-a", f"Should {CHARLIE} co-brand the contract screen?")
    await _sync(figma)
    assert EXTRACTED, "the first sync should extract a graph"

    EXTRACTED.clear()
    await _sync(figma)
    assert EXTRACTED == [], "unchanged files and threads must not be cognified again"

    # A geometry-only save bumps last_modified: the file is fetched again, but
    # its text is identical, so cognee's content hash skips the LLM.
    figma.touch("file-a")
    await _sync(figma)
    assert EXTRACTED == [], "a file whose text did not change must not be cognified again"

    # A new reply changes only that thread, not the file it lives in.
    figma.comment("file-a", "Approved, ship it.", author="Bob", parent=root)
    await _sync(figma)
    assert len(EXTRACTED) == 1 and "Approved, ship it." in EXTRACTED[0]


@pytest.mark.asyncio
async def test_a_file_and_its_comments_share_a_node_set(clean_environment):
    figma = _fake_figma()
    figma.comment("file-a", f"Should {CHARLIE} co-brand the contract screen?")

    await _sync(figma)

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    node_sets = {props.get("name") for _id, props in nodes if props.get("type") == "NodeSet"}
    assert "figma:Logistics portal (file-a)" in node_sets
    assert "figma:Finance prospect (file-b)" in node_sets


@pytest.mark.asyncio
async def test_edited_file_is_re_ingested(clean_environment):
    figma = _fake_figma()
    await _sync(figma)
    before = await _dataset_documents()

    figma.edit("file-b", {"Pitch": [frame("Intro", text(f"{CHARLIE} replaced the prospect."))]})
    await _sync(figma)

    after = await _dataset_documents()
    assert after["file:file-a"] == before["file:file-a"]
    assert await _graph_has(CHARLIE)


# ---------------------------------------------------------------------------
# OAuth integration: picker, stored selection, sync, refresh
# ---------------------------------------------------------------------------


async def _connect(**credential_kwargs):
    """Store a Figma credential for the default user, as the OAuth callback would."""
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.integrations.credentials import upsert_credential

    await setup()
    user = await get_default_user()
    credential_kwargs.setdefault("token_payload", {"access_token": TOKEN})
    return await upsert_credential(
        provider="figma",
        user_id=user.id,
        provider_account_id="user-1",
        account_label="ada@acme.io",
        provider_metadata={"email": "ada@acme.io", "selected_folder_ids": [DESIGN]},
        **credential_kwargs,
    )


@pytest.mark.asyncio
async def test_integration_lists_folders_and_syncs_the_stored_selection(
    clean_environment, monkeypatch
):
    from cognee.modules.integrations.credentials import (
        get_credential_by_account,
        update_provider_metadata,
    )

    monkeypatch.setattr(integration.figma_settings, "team_ids", TEAM)
    figma = _fake_figma()
    figma.add_folder(TEAM, "folder-other", "Marketing")
    credential = await _connect()
    figma_integration = FigmaIntegration(session=figma.session())

    resources = await figma_integration.list_resources(credential)
    assert {resource["id"]: resource["name"] for resource in resources} == {
        DESIGN: "Design",
        "folder-other": "Marketing",
    }

    await figma_integration.sync_now(credential)
    dataset = figma_integration.dataset_name(credential)
    assert set(await _dataset_documents(dataset)) == {"file:file-a", "file:file-b"}
    stored = await get_credential_by_account("figma", "user-1")
    assert stored.sync_status == "ok"

    # Emptying the selection forgets everything the connection synced.
    await update_provider_metadata("figma", "user-1", {"selected_folder_ids": []})
    await figma_integration.sync_now(await get_credential_by_account("figma", "user-1"))
    assert await _dataset_documents(dataset) == {}


@pytest.mark.asyncio
async def test_expiring_token_is_refreshed_before_use(clean_environment, monkeypatch):
    from cognee.modules.integrations.credentials import (
        decrypt_token_payload,
        get_credential_by_account,
    )

    calls = []

    async def _refresh(url, form):
        calls.append((url, form))
        return {"access_token": "fresh-token", "expires_in": 7776000}

    monkeypatch.setattr(integration, "_post_token_request", _refresh)
    credential = await _connect(
        token_payload={"access_token": "stale-token", "refresh_token": "refresh-1"},
        token_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert await integration.access_token_for(credential) == "fresh-token"
    assert calls == [(integration._REFRESH_URL, {"refresh_token": "refresh-1"})]
    stored = decrypt_token_payload(await get_credential_by_account("figma", "user-1"))
    # Figma's refresh token does not rotate, so the stored one is kept.
    assert stored == {"access_token": "fresh-token", "refresh_token": "refresh-1"}


@pytest.mark.asyncio
async def test_packaged_example_runs_end_to_end(clean_environment, monkeypatch, capsys):
    figma = _fake_figma()
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("FIGMA_FOLDER_IDS", DESIGN)

    spec = importlib.util.spec_from_file_location("figma_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    # Route the example's Figma calls to the fake; everything else is real.
    monkeypatch.setattr(
        example,
        "figma_source",
        lambda **kwargs: figma_source(**kwargs, session=figma.session()),
    )

    await example.main()

    assert "Search result:" in capsys.readouterr().out
    assert figma.file_requests(), "the example should have read Figma files"
    assert await _graph_has(ALPHA)
