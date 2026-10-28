"""End-to-end: Lattice changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt replace -> document ingestion -> cognify ->
orphan cleanup) against the fake Lattice API, with a mocked LLM and mocked
embeddings, so no network, Lattice workspace or API key is needed. Each record
carries one distinctive company name, which the mocked LLM turns into exactly
one graph entity.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from fake_lattice import API_KEY, FakeLattice

from cognee_community_connector_lattice import LatticeClient, lattice_source

DATASET = "lattice_forget_test"
ALPHA, BRAVO, CHARLIE, DELTA, ECHO = (
    "Alphacorp",
    "Bravocorp",
    "Charliecorp",
    "Deltacorp",
    "Echocorp",
)


@pytest.fixture
def fake():
    fake = FakeLattice()
    fake.ada = fake.add_user("Ada Lovelace")
    fake.grace = fake.add_user("Grace Hopper")
    fake.alpha = fake.add_goal(f"{ALPHA} renewal", [fake.ada], description="Renews in March.")
    fake.bravo = fake.add_goal(f"{BRAVO} expansion", [fake.grace])
    return fake


async def _sync(fake, dataset=DATASET, **options):
    client = LatticeClient(API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    await cognee.add(lattice_source(client=client, **options), dataset_name=dataset)
    await cognee.cognify(datasets=[dataset])


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


async def _documents(dataset=DATASET) -> dict[str, dict]:
    from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    (found,) = await get_datasets_by_name(dataset, user.id)
    return {
        data.system_metadata.get("external_id"): data.system_metadata
        for data in await get_dataset_data(found.id)
    }


@pytest.mark.asyncio
async def test_goals_are_ingested_and_deleted_goals_are_forgotten(mocked_llm, fake):
    await _sync(fake)

    for token in (ALPHA, BRAVO):
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    documents = await _documents()
    assert documents[f"lattice:goal:{fake.alpha}"]["source"] == "lattice"
    assert documents[f"lattice:goal:{fake.alpha}"]["table_name"] == "lattice_goals"

    # Upstream: one goal deleted, one edited, one added.
    fake.delete_goal(fake.bravo)
    fake.edit_goal(fake.alpha, name=f"{DELTA} renewal")
    fake.add_goal(f"{ECHO} launch", [fake.ada])
    await _sync(fake)

    assert not await _graph_has(BRAVO), "a deleted goal must be forgotten from the graph"
    assert await _graph_has(DELTA), "an edited goal must be re-ingested"
    assert await _graph_has(ECHO), "a new goal must be ingested"
    assert f"lattice:goal:{fake.bravo}" not in await _documents()


@pytest.mark.asyncio
async def test_the_last_goal_being_deleted_is_forgotten_too(mocked_llm, fake):
    fake.delete_goal(fake.bravo)
    await _sync(fake)
    assert await _graph_has(ALPHA)

    fake.delete_goal(fake.alpha)
    await _sync(fake)

    assert not await _graph_has(ALPHA), "an emptied table must still forget its last goal"
    assert await _documents() == {}


@pytest.mark.asyncio
async def test_an_archived_or_privatized_goal_is_forgotten(mocked_llm, fake):
    await _sync(fake)
    fake.edit_goal(fake.alpha, state="Archived")
    fake.edit_goal(fake.bravo, isPrivate=True)
    await _sync(fake)

    assert not await _graph_has(ALPHA)
    assert not await _graph_has(BRAVO)


@pytest.mark.asyncio
async def test_a_new_check_in_is_picked_up_though_the_goals_updated_at_did_not_move(
    mocked_llm, fake
):
    await _sync(fake)
    updated_at = fake.goals[fake.alpha]["updatedAt"]
    assert not await _graph_has(CHARLIE)

    fake.add_goal_update(fake.alpha, f"Signed {CHARLIE} as a reference.", "green")
    assert fake.goals[fake.alpha]["updatedAt"] == updated_at

    await _sync(fake)
    assert await _graph_has(CHARLIE)


@pytest.mark.asyncio
async def test_an_unchanged_resync_does_not_call_the_llm_again(mocked_llm, fake):
    await _sync(fake)
    assert mocked_llm, "the first sync should extract a graph"

    mocked_llm.clear()
    await _sync(fake)
    assert mocked_llm == [], "unchanged goals must not be cognified again"

    # A save that changes no text bumps updatedAt: the goal is listed again, but
    # its document is identical, so cognee's content hash skips the LLM.
    fake.touch_goal(fake.alpha)
    await _sync(fake)
    assert mocked_llm == [], "a goal whose text did not change must not be cognified again"

    fake.edit_goal(fake.alpha, description="Renewed early.")
    await _sync(fake)
    assert len(mocked_llm) == 1 and "Renewed early." in mocked_llm[0]


@pytest.mark.asyncio
async def test_goals_share_a_node_set(mocked_llm, fake):
    await _sync(fake)

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    node_sets = {props.get("name") for _id, props in nodes if props.get("type") == "NodeSet"}
    assert "lattice:goals" in node_sets


@pytest.mark.asyncio
async def test_feedback_stays_out_of_memory_until_it_is_asked_for_and_leaves_when_withdrawn(
    mocked_llm, fake
):
    fake.add_feedback(fake.ada, [fake.grace], f"{CHARLIE} demo was excellent.")
    fake.add_feedback(fake.grace, [fake.ada], f"{DELTA} needs improvement.", public=False)

    await _sync(fake)
    assert not await _graph_has(CHARLIE), "feedback is off by default"

    await _sync(fake, include_feedback=True)
    assert await _graph_has(CHARLIE), "public feedback is ingested once asked for"
    assert not await _graph_has(DELTA), "private feedback needs feedback_only_public=False"

    await _sync(fake, include_feedback=True, feedback_only_public=False)
    assert await _graph_has(DELTA)

    # Narrowing the scope forgets what the wider scope let in.
    await _sync(fake, include_feedback=True)
    assert not await _graph_has(DELTA), "dropping private feedback must purge it"
    await _sync(fake)
    assert not await _graph_has(CHARLIE), "switching feedback off must purge it"
    assert await _graph_has(ALPHA), "goals are untouched"


@pytest.mark.asyncio
async def test_deleted_feedback_is_forgotten(mocked_llm, fake):
    note = fake.add_feedback(fake.ada, [fake.grace], f"{CHARLIE} demo was excellent.")
    await _sync(fake, include_feedback=True)
    assert await _graph_has(CHARLIE)

    fake.delete_feedback(note)
    await _sync(fake, include_feedback=True)
    assert not await _graph_has(CHARLIE)


@pytest.fixture
def reviewed(fake):
    fake.cycle = fake.add_cycle(f"{ECHO} H1 2026")
    fake.reviewee = fake.add_reviewee(fake.cycle, fake.grace)
    fake.question = fake.add_question("What went well?")
    fake.review = fake.add_review(
        fake.cycle, fake.reviewee, fake.question, fake.ada, comment=f"Closed {CHARLIE} early."
    )
    return fake


@pytest.mark.asyncio
async def test_review_cycles_are_in_but_responses_need_the_cycle_selected(mocked_llm, reviewed):
    await _sync(reviewed)
    assert await _graph_has(ECHO), "the cycle itself is ingested by default"
    assert not await _graph_has(CHARLIE), "review responses are off by default"

    await _sync(reviewed, review_cycle_ids=[reviewed.cycle])
    assert await _graph_has(CHARLIE)
    documents = await _documents()
    assert f"lattice:review:{reviewed.cycle}:{reviewed.reviewee}" in documents

    # Taking the cycle out of the selection forgets its responses, not the cycle.
    await _sync(reviewed)
    assert not await _graph_has(CHARLIE)
    assert await _graph_has(ECHO)


@pytest.mark.asyncio
async def test_an_edited_review_is_reingested_and_a_deleted_one_forgotten(mocked_llm, reviewed):
    await _sync(reviewed, review_cycle_ids=[reviewed.cycle])
    assert await _graph_has(CHARLIE)

    reviewed.edit_review(reviewed.review, comment=f"Closed {DELTA} early.")
    await _sync(reviewed, review_cycle_ids=[reviewed.cycle])
    assert await _graph_has(DELTA)
    assert not await _graph_has(CHARLIE)

    reviewed.delete_review(reviewed.review)
    await _sync(reviewed, review_cycle_ids=[reviewed.cycle])
    assert not await _graph_has(DELTA), "a reviewee with no readable review has no document"


@pytest.mark.asyncio
async def test_a_deleted_review_cycle_is_forgotten(mocked_llm, reviewed):
    await _sync(reviewed)
    assert await _graph_has(ECHO)

    reviewed.delete_cycle(reviewed.cycle)
    await _sync(reviewed)
    assert not await _graph_has(ECHO)


@pytest.mark.asyncio
async def test_a_failed_sync_forgets_nothing_and_the_next_one_catches_up(mocked_llm, fake):
    fake.add_feedback(fake.ada, [fake.grace], f"{CHARLIE} demo was excellent.")
    await _sync(fake, include_feedback=True)
    assert await _graph_has(BRAVO) and await _graph_has(CHARLIE)

    # Goals read fine (and one is gone), then the feedback listing never recovers:
    # a cut-off run must not be read as "all the feedback was deleted".
    fake.delete_goal(fake.bravo)
    fake.fail("/v1/feedbacks", 503, times=50)
    with pytest.raises(Exception, match="503"):
        await _sync(fake, include_feedback=True)

    assert await _graph_has(BRAVO), "nothing may be forgotten by a failed run"
    assert await _graph_has(CHARLIE)

    fake._failures.clear()
    await _sync(fake, include_feedback=True)
    assert not await _graph_has(BRAVO), "the next healthy run applies the deletion"
    assert await _graph_has(CHARLIE)


@pytest.mark.asyncio
async def test_a_rate_limit_that_does_not_clear_forgets_nothing(mocked_llm, fake):
    await _sync(fake)
    fake.fail("/v1/goals", 429, times=50, headers={"Retry-After": "1"})
    with pytest.raises(Exception, match="429"):
        await _sync(fake)
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)


@pytest.mark.asyncio
async def test_a_merge_disposition_is_refused_instead_of_never_forgetting(mocked_llm, fake):
    client = LatticeClient(API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    with pytest.raises(Exception, match='write_disposition="replace"'):
        await cognee.add(
            lattice_source(client=client),
            dataset_name=DATASET,
            primary_key="id",
            write_disposition="merge",
        )
    assert fake.calls == [], "nothing may be read before the refusal"


@pytest.mark.asyncio
async def test_two_workspaces_in_one_dataset_do_not_forget_each_other(mocked_llm, fake):
    emea = FakeLattice()
    boss = emea.add_user("Eve Employee")
    emea.add_goal(f"{CHARLIE} rollout", [boss])

    await _sync(fake, resource_name="us_lattice")
    await _sync(emea, resource_name="emea_lattice")
    assert await _graph_has(ALPHA) and await _graph_has(CHARLIE)

    fake.delete_goal(fake.alpha)
    await _sync(fake, resource_name="us_lattice")
    assert not await _graph_has(ALPHA)
    assert await _graph_has(CHARLIE), "the other workspace's goals must stay"


@pytest.mark.asyncio
async def test_it_works_through_cognee_remember_with_the_default_disposition(mocked_llm, fake):
    client = LatticeClient(API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    await cognee.remember(lattice_source(client=client), dataset_name=DATASET)

    assert await _graph_has(ALPHA)
