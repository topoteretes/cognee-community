"""The source: what it selects, what it refuses by default, and how it fails."""

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    PIPELINE_SCOPE_ATTR,
)
from dlt.extract.exceptions import ResourceExtractionError
from fake_lattice import API_KEY, FakeLattice

from cognee_community_connector_lattice import LatticeAPIError, LatticeClient, lattice_source


def source_for(fake: FakeLattice, **options):
    client = LatticeClient(API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    return lattice_source(client=client, **options)


def rows(source, kind: str, name: str = "lattice") -> list[dict]:
    try:
        return list(source.resources[f"{name}_{kind}"])
    except ResourceExtractionError as exc:
        # dlt wraps whatever a resource raises; the tests care about the original.
        raise exc.__cause__ from None


def by_title(rows_: list[dict]) -> dict[str, dict]:
    return {row["title"]: row for row in rows_}


@pytest.fixture
def fake():
    fake = FakeLattice()
    fake.ada = fake.add_user("Ada Lovelace")
    fake.grace = fake.add_user("Grace Hopper")
    return fake


# -- declaration ---------------------------------------------------------------
def test_the_source_opts_into_document_mode_with_its_own_pipeline_scope(fake):
    source = source_for(fake, resource_name="acme_lattice")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "lattice"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "acme_lattice"
    assert sorted(source.resources) == [
        "acme_lattice_feedback",
        "acme_lattice_goals",
        "acme_lattice_review_cycles",
        "acme_lattice_reviews",
    ]


def test_all_four_tables_exist_even_when_nothing_is_selected_for_them(fake):
    # cognee only reconciles tables the run still names, so a switched-off kind
    # must stay a (now empty) resource instead of disappearing.
    source = source_for(fake, include_review_cycles=False)
    assert len(source.resources) == 4
    assert rows(source, "review_cycles") == []
    assert rows(source, "feedback") == []
    assert rows(source, "reviews") == []


def test_every_row_carries_its_own_node_set_and_a_scoped_id(fake):
    fake.add_goal("Ship", [fake.ada])
    (row,) = rows(source_for(fake), "goals")
    assert row["id"].startswith("lattice:goal:")
    assert row[NODE_SET_COLUMN] == ["goals"]
    assert row["url"].startswith("https://lattice.test/goals/")


# -- goals -----------------------------------------------------------------------
def test_goals_carry_owner_names_parent_and_progress_updates(fake):
    parent = fake.add_goal("Company plan", [fake.ada])
    child = fake.add_goal("Alphacorp renewal", [fake.ada, fake.grace], parent=parent)
    fake.add_goal_update(child, "Kick-off done", "green", increment="10.000")

    goal = by_title(rows(source_for(fake), "goals"))["Alphacorp renewal"]

    assert "Owners: Ada Lovelace, Grace Hopper" in goal["content"]
    assert "Parent goal: Company plan" in goal["content"]
    assert "- green (increment 10.000): Kick-off done" in goal["content"]


def test_a_goal_without_updates_costs_no_extra_request_when_updates_are_off(fake):
    fake.add_goal("Ship", [fake.ada])
    rows(source_for(fake, include_goal_updates=False), "goals")
    assert fake.requests_to("/updates") == []


def test_by_default_only_active_and_ended_goals_are_read_one_state_at_a_time(fake):
    for state in ("Draft", "Active", "Ended", "Archived"):
        fake.add_goal(f"{state} goal", [fake.ada], state=state)
    source = source_for(fake)

    assert sorted(by_title(rows(source, "goals"))) == ["Active goal", "Ended goal"]
    assert {c[2]["state"] for c in fake.requests_to("/v1/goals") if "state" in c[2]} == {
        "Active",
        "Ended",
    }


def test_goal_states_can_be_widened_to_archived(fake):
    fake.add_goal("Old", [fake.ada], state="Archived")
    source = source_for(fake, goal_states=["Archived"])
    assert list(by_title(rows(source, "goals"))) == ["Old"]


def test_private_goals_are_skipped_unless_asked_for(fake):
    fake.add_goal("Public", [fake.ada])
    fake.add_goal("Secret plan", [fake.ada], private=True)

    assert list(by_title(rows(source_for(fake), "goals"))) == ["Public"]
    both = by_title(rows(source_for(fake, include_private_goals=True), "goals"))
    assert sorted(both) == ["Public", "Secret plan"]


def test_a_skipped_private_parent_does_not_leak_its_name_through_its_children(fake):
    secret = fake.add_goal("Layoff plan", [fake.ada], private=True)
    fake.add_goal("Public child", [fake.ada], parent=secret)

    (child,) = rows(source_for(fake), "goals")
    assert "Layoff plan" not in child["content"]
    assert "Parent goal" not in child["content"]


def test_a_server_that_ignores_the_state_filter_cannot_widen_the_scope(fake):
    fake.add_goal("Draft goal", [fake.ada], state="Draft")
    fake.add_goal("Active goal", [fake.ada])
    source = source_for(fake)
    # Every state query answers with every goal, as if the filter were ignored.
    fake._route_original = fake._route

    def ignore_state(path, params):
        return fake._route_original(path, {k: v for k, v in params.items() if k != "state"})

    fake._route = ignore_state
    assert list(by_title(rows(source, "goals"))) == ["Active goal"]


def test_an_owner_missing_from_the_user_list_is_not_named(fake):
    fake.add_goal("Ship", ["no-such-user"])
    (goal,) = rows(source_for(fake), "goals")
    assert "Owners: an unknown user" in goal["content"]


def test_a_deactivated_owner_is_still_named(fake):
    gone = fake.add_user("Alan Turing", status="DEACTIVATED")
    fake.add_goal("Legacy work", [gone])
    (goal,) = rows(source_for(fake), "goals")
    assert "Owners: Alan Turing" in goal["content"]
    assert any(c[2].get("status") == "NULL" for c in fake.requests_to("/v1/users"))


def test_a_goal_deleted_between_listing_and_its_updates_is_left_out_not_fatal(fake):
    keep = fake.add_goal("Keep", [fake.ada])
    drop = fake.add_goal("Drop", [fake.ada])

    def delete_on_first_update(path, _params):
        if path.endswith("/updates") and drop in fake.goals:
            fake.delete_goal(drop)

    fake.on_request = delete_on_first_update
    titles = [r["title"] for r in rows(source_for(fake), "goals")]
    assert "Keep" in titles and "Drop" not in titles
    assert keep in fake.goals


def test_a_failed_goal_listing_aborts_instead_of_yielding_a_partial_snapshot(fake):
    for i in range(3):
        fake.add_goal(f"Goal {i}", [fake.ada])
    fake.fail("/v1/goals", 500, times=20)
    with pytest.raises(LatticeAPIError, match="500"):
        rows(source_for(fake), "goals")


def test_a_failure_while_reading_updates_aborts_the_whole_kind(fake):
    for i in range(3):
        fake.add_goal(f"Goal {i}", [fake.ada])
    fake.fail("/v1/goals/", 429, times=20, headers={"Retry-After": "1"})
    with pytest.raises(LatticeAPIError, match="429"):
        rows(source_for(fake), "goals")


# -- feedback --------------------------------------------------------------------
def test_feedback_is_off_by_default_and_never_even_requested(fake):
    fake.add_feedback(fake.ada, [fake.grace], "Great demo")
    source = source_for(fake)
    assert rows(source, "feedback") == []
    assert rows(source, "goals") == []
    assert fake.requests_to("/v1/feedbacks") == []


def test_feedback_when_enabled_is_public_only_and_asks_the_server_for_that(fake):
    fake.add_feedback(fake.ada, [fake.grace], "Great demo")
    fake.add_feedback(fake.grace, [fake.ada], "Private note about performance", public=False)
    result = rows(source_for(fake, include_feedback=True), "feedback")

    assert [r["title"] for r in result] == ["Feedback from Ada Lovelace to Grace Hopper"]
    assert "Great demo" in result[0]["content"]
    assert fake.requests_to("/v1/feedbacks")[0][2]["onlyPublic"] == "true"


def test_feedback_that_slips_past_a_server_side_filter_is_still_dropped(fake):
    fake.add_feedback(fake.grace, [fake.ada], "Private note", public=False)
    source = source_for(fake, include_feedback=True)
    original = fake._route
    fake._route = lambda path, params: original(path, {**params, "onlyPublic": "false"})
    assert rows(source, "feedback") == []


def test_private_feedback_needs_an_explicit_opt_out_of_public_only(fake):
    fake.add_feedback(fake.ada, [fake.grace], "Great demo")
    fake.add_feedback(fake.grace, [fake.ada], "Private note", public=False)
    source = source_for(fake, include_feedback=True, feedback_only_public=False)

    assert len(rows(source, "feedback")) == 2
    assert "onlyPublic" not in fake.requests_to("/v1/feedbacks")[0][2]


# -- review cycles and reviews -----------------------------------------------------
def test_review_cycles_are_listed_without_drafts_or_responses(fake):
    fake.add_cycle("H1 2026")
    fake.add_cycle("Secret future cycle", stage="Draft")
    result = rows(source_for(fake), "review_cycles")

    assert [r["title"] for r in result] == ["Review cycle: H1 2026"]
    assert rows(source_for(fake), "reviews") == []
    assert fake.requests_to("/reviews") == []


@pytest.fixture
def reviewed(fake):
    fake.cycle = fake.add_cycle("H1 2026")
    fake.reviewee = fake.add_reviewee(fake.cycle, fake.grace)
    fake.q_well = fake.add_question("What went well?")
    fake.q_rate = fake.add_question("Rate collaboration")
    fake.peer = fake.add_user("Pat Peer")
    fake.add_review(
        fake.cycle, fake.reviewee, fake.q_well, fake.peer, "Peer", comment="Shipped the migration"
    )
    fake.add_review(
        fake.cycle,
        fake.reviewee,
        fake.q_rate,
        fake.ada,
        "Downward",
        rating=4,
        comment="Strong",
        index=1,
    )
    return fake


def test_review_responses_are_one_document_per_reviewee_for_the_selected_cycle(reviewed):
    (row,) = rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")

    assert row["title"] == "Review of Grace Hopper in H1 2026"
    assert row["id"] == f"lattice:review:{reviewed.cycle}:{reviewed.reviewee}"
    assert "Question: What went well?" in row["content"]
    assert "Peer review: comment: Shipped the migration" in row["content"]
    assert "Downward review: rating 4; comment: Strong" in row["content"]


def test_no_reviewer_is_named_anywhere_in_a_review_document(reviewed):
    (row,) = rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")
    haystack = " ".join(str(v) for v in row.values())
    assert "Pat Peer" not in haystack
    assert "Ada Lovelace" not in haystack


def test_only_submitted_visible_reviews_are_ingested(reviewed):
    reviewed.add_review(
        reviewed.cycle,
        reviewed.reviewee,
        reviewed.q_well,
        reviewed.peer,
        comment="DRAFT ONLY",
        submitted=False,
    )
    reviewed.add_review(
        reviewed.cycle,
        reviewed.reviewee,
        reviewed.q_well,
        reviewed.peer,
        comment="DECLINED",
        declined=True,
    )
    reviewed.add_review(
        reviewed.cycle,
        reviewed.reviewee,
        reviewed.q_well,
        reviewed.peer,
        comment="HIDDEN",
        visible=False,
    )
    (row,) = rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")

    for leaked in ("DRAFT ONLY", "DECLINED", "HIDDEN"):
        assert leaked not in row["content"]


def test_a_reviewee_whose_reviews_are_all_unreadable_has_no_document(reviewed):
    other = reviewed.add_reviewee(reviewed.cycle, reviewed.ada)
    reviewed.add_review(
        reviewed.cycle, other, reviewed.q_well, reviewed.peer, comment="x", visible=False
    )
    result = rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")
    assert [r["title"] for r in result] == ["Review of Grace Hopper in H1 2026"]


def test_only_the_selected_cycles_responses_are_read(reviewed):
    other = reviewed.add_cycle("H2 2026")
    reviewed.add_reviewee(other, reviewed.ada)
    rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")

    assert reviewed.requests_to(f"/reviewCycle/{other}") == []
    assert reviewed.requests_to(f"/reviewCycle/{reviewed.cycle}/reviews")


def test_an_unknown_cycle_id_stops_the_sync_and_lists_the_ones_that_exist(reviewed):
    with pytest.raises(ValueError, match="not found") as caught:
        rows(source_for(reviewed, review_cycle_ids=["nope"]), "reviews")
    assert "H1 2026" in str(caught.value)


def test_a_question_is_read_once_however_many_reviews_use_it(reviewed):
    for _ in range(5):
        reviewed.add_review(
            reviewed.cycle, reviewed.reviewee, reviewed.q_well, reviewed.peer, comment="again"
        )
    rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")
    assert len(reviewed.requests_to("/questionRevision/")) == 2


# -- arguments and safety --------------------------------------------------------------
def test_only_get_requests_are_ever_sent(reviewed):
    source = source_for(
        reviewed,
        include_feedback=True,
        feedback_only_public=False,
        review_cycle_ids=[reviewed.cycle],
    )
    for kind in ("goals", "feedback", "review_cycles", "reviews"):
        rows(source, kind)
    assert {method for method, _path, _params in reviewed.calls} == {"GET"}


def test_unchanged_data_renders_identical_rows_on_every_run(reviewed):
    reviewed.add_goal("Ship", [reviewed.ada])
    options = {"include_feedback": True, "review_cycle_ids": [reviewed.cycle]}
    first = {k: rows(source_for(reviewed, **options), k) for k in ("goals", "reviews")}
    second = {k: rows(source_for(reviewed, **options), k) for k in ("goals", "reviews")}
    assert first == second


def test_the_api_key_is_never_in_a_row(reviewed):
    reviewed.add_goal("Ship", [reviewed.ada])
    source = source_for(reviewed, include_feedback=True, review_cycle_ids=[reviewed.cycle])
    everything = [
        row
        for kind in ("goals", "feedback", "review_cycles", "reviews")
        for row in rows(source, kind)
    ]
    assert API_KEY not in str(everything)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"resource_name": "Bad Name"}, "resource_name"),
        ({"goal_states": ["Done"]}, "goal_states"),
        ({"goal_states": []}, "goal_states"),
        ({"review_cycle_ids": []}, "review_cycle_ids is empty"),
        (
            {"include_goals": False, "include_review_cycles": False},
            "Nothing is selected",
        ),
    ],
)
def test_bad_selections_are_refused_before_anything_is_read(fake, options, message):
    with pytest.raises(ValueError, match=message):
        source_for(fake, **options)
    assert fake.calls == []


def test_a_single_string_is_accepted_for_the_list_arguments(reviewed):
    source = source_for(reviewed, goal_states="Active", review_cycle_ids=reviewed.cycle)
    assert len(rows(source, "reviews")) == 1


def test_a_non_replace_disposition_is_refused_before_reading_anything(fake):
    fake.add_goal("Ship", [fake.ada])
    source = source_for(fake)
    source.resources["lattice_goals"].apply_hints(write_disposition="merge")
    with pytest.raises(ValueError, match='needs write_disposition="replace"'):
        rows(source, "goals")
    assert fake.calls == []


def test_the_source_needs_a_key_when_no_client_is_given(monkeypatch):
    monkeypatch.delenv("LATTICE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LATTICE_API_KEY"):
        lattice_source()


# -- hardening ---------------------------------------------------------------------
def test_an_unreadable_updates_endpoint_keeps_the_goal_instead_of_forgetting_it(fake):
    goal = fake.add_goal("Alphacorp renewal", [fake.ada])
    fake.fail(f"/v1/goals/{goal}/updates", 404, times=20)

    (row,) = rows(source_for(fake), "goals")
    assert row["title"] == "Alphacorp renewal"
    assert "Progress updates" not in row["content"]


def test_a_forbidden_updates_endpoint_also_keeps_the_goal(fake):
    goal = fake.add_goal("Ship", [fake.ada])
    fake.fail(f"/v1/goals/{goal}/updates", 403, times=20)
    assert len(rows(source_for(fake), "goals")) == 1


def test_a_goal_the_server_confirms_gone_is_left_out(fake):
    fake.add_goal("Keep", [fake.ada])
    drop = fake.add_goal("Drop", [fake.ada])
    fake.fail(f"/v1/goals/{drop}/updates", 404, times=20)
    del fake.goals[drop]  # the single-goal lookup now answers 404 too

    assert [r["title"] for r in rows(source_for(fake), "goals")] == ["Keep"]


def test_a_server_error_on_updates_still_aborts_the_run(fake):
    goal = fake.add_goal("Ship", [fake.ada])
    fake.fail(f"/v1/goals/{goal}/updates", 500, times=20)
    with pytest.raises(LatticeAPIError, match="500"):
        rows(source_for(fake), "goals")


def test_a_workspace_that_refuses_status_null_falls_back_to_active_users(fake):
    gone = fake.add_user("Alan Turing", status="DEACTIVATED")
    fake.add_goal("Legacy", [gone, fake.ada])
    original = fake._route

    def refuse_null(path, params):
        if path == "/v1/users" and params.get("status") == "NULL":
            return httpx.Response(400, json={"error": "invalid status"})
        return original(path, params)

    fake._route = refuse_null
    (row,) = rows(source_for(fake), "goals")
    assert "Owners: Ada Lovelace, an unknown user" in row["content"]


def test_an_empty_user_list_stops_the_sync_instead_of_reading_every_list_as_empty():
    fake = FakeLattice()  # no users: a key that can see nothing
    with pytest.raises(LatticeAPIError, match="no users for this API key"):
        rows(source_for(fake), "goals")


def test_an_unreadable_question_falls_back_to_its_id_and_does_not_stop_the_sync(reviewed):
    del reviewed.questions[reviewed.q_well]
    (row,) = rows(source_for(reviewed, review_cycle_ids=[reviewed.cycle]), "reviews")
    assert f"Question: {reviewed.q_well}" in row["content"]
    assert "Shipped the migration" in row["content"]


def test_a_source_can_be_read_twice_and_closes_its_connection_each_time(fake):
    fake.add_goal("Ship", [fake.ada])
    client = LatticeClient(API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    source = lattice_source(client=client)

    assert len(rows(source, "goals")) == 1
    assert client._http is None, "the connection is closed after each kind"
    assert len(rows(source, "goals")) == 1


def test_check_active_stops_a_source_before_it_reads_more(fake):
    fake.add_goal("Ship", [fake.ada])
    stop = {"now": False}

    def check():
        if stop["now"]:
            raise PermissionError("access revoked")

    source = source_for(fake, check_active=check)
    stop["now"] = True
    with pytest.raises(PermissionError):
        rows(source, "goals")
    assert fake.calls == []


def test_sync_stats_are_counts_only(reviewed):
    reviewed.add_goal("Alphacorp secret title", [reviewed.ada])
    reviewed.add_goal("Hidden", [reviewed.ada], private=True)
    source = source_for(reviewed, review_cycle_ids=[reviewed.cycle])
    for kind in ("goals", "review_cycles", "reviews"):
        rows(source, kind)

    assert source.cognee_sync_stats == {
        "goals": 1,
        "goals_skipped": 1,
        "review_cycles": 1,
        "reviewees": 1,
    }
