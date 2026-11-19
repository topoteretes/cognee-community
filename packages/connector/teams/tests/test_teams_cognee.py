"""End-to-end: the Teams connector through the real ``cognee.add`` + ``cognify``.

Graph is served by ``FakeGraph`` and the LLM and embeddings are mocked, so no
Microsoft 365 tenant or API key is needed. Verifies the acceptance criteria at
the memory layer:

* threads are ingested as documents (``source="teams"``) and cognified;
* an unchanged re-sync makes no LLM call and keeps every ``data_id``;
* an edited thread is replaced and re-extracted;
* deleting a thread upstream removes its extracted entity from the graph on
  the next sync (forget-on-delete), including the last remaining thread;
* the packaged ``examples/example.py`` runs end to end.
"""

import importlib.util
import pathlib

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_graph import FakeGraph

from cognee_community_connector_teams import teams_source

DATASET = "teams_e2e_test"
TEAM = "team-1"
GENERAL = "19:general@thread.tacv2"
ALPHA, BRAVO = ENTITY_TOKENS
EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _thread_id(root_id):
    return f"thread:{TEAM}:{GENERAL}:{root_id}"


def _fake_graph() -> FakeGraph:
    graph = FakeGraph()
    graph.add_team(TEAM, "Engineering")
    graph.add_channel(TEAM, GENERAL, "General")
    return graph


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


async def _documents() -> dict[str, object]:
    """Return {external_id: data_id} for the Data records in the test dataset."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "teams"
    }


async def _sync(graph: FakeGraph) -> None:
    await cognee.add(
        teams_source("tenant", "client", "secret", team_ids=[TEAM], session=graph.session()),
        dataset_name=DATASET,
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_thread_forgets_its_graph_content(mocked_llm):
    graph = _fake_graph()
    alpha = graph.post(TEAM, GENERAL, f"{ALPHA} signed the logistics contract.")
    graph.reply(TEAM, GENERAL, alpha, "Great news, kickoff is next week.")
    bravo = graph.post(TEAM, GENERAL, f"{BRAVO} is an unrelated finance prospect.")

    await _sync(graph)
    documents = await _documents()
    assert set(documents) == {_thread_id(alpha), _thread_id(bravo)}
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)

    # Delete Bravo's thread upstream; the incremental sync must forget it.
    graph.delete(TEAM, GENERAL, bravo)
    await _sync(graph)

    after = await _documents()
    assert set(after) == {_thread_id(alpha)}
    # The unchanged thread kept its content-hash data_id (not re-ingested).
    assert after[_thread_id(alpha)] == documents[_thread_id(alpha)]
    assert await _graph_has(ALPHA), "surviving thread's entity must remain"
    assert not await _graph_has(BRAVO), "deleted thread's entity must be forgotten"


@pytest.mark.asyncio
async def test_unchanged_resync_makes_no_llm_call(mocked_llm):
    graph = _fake_graph()
    graph.post(TEAM, GENERAL, f"{ALPHA} signed the logistics contract.")
    graph.post(TEAM, GENERAL, f"{BRAVO} is an unrelated finance prospect.")

    await _sync(graph)
    documents = await _documents()
    extractions = len(mocked_llm)
    assert extractions >= 2

    # The cursor's overlap window re-reads the newest threads; they render
    # identically, so cognee must keep them as they are.
    await _sync(graph)

    assert await _documents() == documents
    assert len(mocked_llm) == extractions, "an unchanged re-sync must not call the LLM"


@pytest.mark.asyncio
async def test_edited_thread_is_replaced_and_re_extracted(mocked_llm):
    graph = _fake_graph()
    graph.post(TEAM, GENERAL, f"{ALPHA} signed the logistics contract.")
    bravo = graph.post(TEAM, GENERAL, f"{BRAVO} is an unrelated finance prospect.")
    await _sync(graph)
    before = await _documents()
    extractions = len(mocked_llm)

    graph.edit(TEAM, GENERAL, bravo, f"{BRAVO} is now a paying customer.")
    await _sync(graph)

    after = await _documents()
    assert set(after) == set(before)
    assert after[_thread_id(bravo)] != before[_thread_id(bravo)]
    # Only the edited thread is extracted again; Alpha is untouched.
    assert len(mocked_llm) == extractions + 1
    assert any("paying customer" in text for text in mocked_llm[extractions:])


@pytest.mark.asyncio
async def test_the_last_thread_is_forgotten_too(mocked_llm):
    graph = _fake_graph()
    only = graph.post(TEAM, GENERAL, f"{ALPHA} signed the logistics contract.")
    await _sync(graph)
    assert set(await _documents()) == {_thread_id(only)}
    assert await _graph_has(ALPHA)

    graph.delete(TEAM, GENERAL, only)
    await _sync(graph)

    assert await _documents() == {}
    assert not await _graph_has(ALPHA)


@pytest.mark.asyncio
async def test_packaged_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    graph = _fake_graph()
    graph.post(TEAM, GENERAL, f"{ALPHA} signed the logistics contract.")
    monkeypatch.setenv("MS_GRAPH_TENANT_ID", "tenant")
    monkeypatch.setenv("MS_GRAPH_CLIENT_ID", "client")
    monkeypatch.setenv("MS_GRAPH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TEAMS_TEAM_ID", TEAM)

    spec = importlib.util.spec_from_file_location("teams_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    # Route the example's Graph calls to the fake; everything else is real.
    monkeypatch.setattr(
        example,
        "teams_source",
        lambda **kwargs: teams_source(**kwargs, session=graph.session()),
    )

    await example.main()

    assert "Search result:" in capsys.readouterr().out
    assert graph.message_requests(), "the example should have read channel messages"
    assert await _graph_has(ALPHA)
