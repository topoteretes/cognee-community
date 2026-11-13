"""Rendering: deterministic text, readable fields, no reviewer names."""

from cognee_community_connector_lattice.render import (
    cycle_content,
    feedback_content,
    feedback_title,
    goal_content,
    goal_title,
    reviewee_content,
    when,
)

GOAL = {
    "id": "g1",
    "name": "Grow EMEA revenue",
    "description": "  Land three enterprise accounts.  ",
    "state": "Active",
    "status": "OffTrack",
    "goalType": "Company",
    "okrType": "key_result",
    "priority": 2,
    "amountType": "Percent",
    "startingAmount": 0,
    "currentAmount": 40.0,
    "endingAmount": 100,
    "startDate": "2026-01-01",
    "dueDate": "2026-03-31",
}


def test_a_goal_reads_as_plain_sentences_with_its_check_ins():
    updates = [
        {"id": 2, "status": "red", "comment": "Slipped a week", "increment": None},
        {"id": 1, "status": "green", "comment": "Kick-off done", "increment": "10.000"},
    ]
    text = goal_content(GOAL, ["Grace Hopper", "Ada Lovelace"], "Company plan", updates)

    assert goal_title(GOAL) == "Grow EMEA revenue"
    assert text.splitlines() == [
        "Type: company key result",
        "Owners: Ada Lovelace, Grace Hopper",
        "State: Active. Status: off track.",
        "Parent goal: Company plan",
        "Start: 2026-01-01. Due: 2026-03-31.",
        "Progress: 40 of 100 (percent), started at 0",
        "Priority: 2",
        "Description: Land three enterprise accounts.",
        "Progress updates:",
        "- green (increment 10.000): Kick-off done",
        "- red: Slipped a week",
    ]


def test_the_same_data_always_renders_the_same_text():
    updates = [{"id": 1, "status": "green", "comment": "a"}, {"id": 2, "status": "red"}]
    first = goal_content(GOAL, ["B", "A"], None, updates)
    assert first == goal_content(GOAL, ["A", "B"], None, list(reversed(updates)))


def test_a_new_check_in_changes_the_text_even_though_updated_at_is_not_in_it():
    before = goal_content({**GOAL, "updatedAt": 1}, ["A"], None, [])
    after = goal_content({**GOAL, "updatedAt": 1}, ["A"], None, [{"id": 1, "comment": "new"}])
    assert before != after
    # ...and a save that changed nothing does not.
    assert before == goal_content({**GOAL, "updatedAt": 99}, ["A"], None, [])


def test_a_binary_goal_has_no_amount_line():
    text = goal_content({**GOAL, "amountType": "Binary", "currentAmount": None}, [], None, [])
    assert "Progress:" not in text


def test_a_sparse_goal_still_renders():
    text = goal_content({"id": "g", "name": "x"}, [], None, [])
    assert text == "Type: goal"
    assert goal_title({"id": "g"}) == "Goal g"


def test_timestamps_are_utc_dates_and_tolerate_milliseconds():
    assert when(1767225600) == "2026-01-01"
    assert when(1767225600000) == "2026-01-01"
    assert when(None) is None
    assert when(0) is None
    assert when(True) is None
    assert when("2026-01-01") is None


def test_feedback_names_author_and_targets_and_its_visibility():
    feedback = {
        "body": " Great demo. ",
        "isPublic": True,
        "createdAt": 1767225600,
        "associatedValues": {"data": [{"name": "Candor"}, {"name": "Craft"}]},
    }
    assert feedback_title("Ada", ["Grace", "Alan"]) == "Feedback from Ada to Alan, Grace"
    assert feedback_content(feedback).splitlines() == [
        "Given: 2026-01-01",
        "Shared with the whole company",
        "Company values: Candor, Craft",
        "Feedback: Great demo.",
    ]
    assert "Private" in feedback_content({"body": "x", "isPublic": False})
    assert feedback_title("Ada", []) == "Feedback from Ada to an unknown user"


def test_a_review_cycle_lists_stage_and_dates():
    text = cycle_content(
        {"stage": "CollectingReviews", "reviewsStartedAt": 1767225600, "calibrationEnabled": True}
    )
    assert text.splitlines() == [
        "Stage: Collecting reviews",
        "Reviews started: 2026-01-01",
        "Calibration: enabled",
    ]


def test_reviews_are_grouped_by_question_and_never_name_the_reviewer():
    questions = {"q1": {"body": "What went well?"}, "q2": {"body": "Rate collaboration"}}
    reviews = [
        {
            "id": "r2",
            "questionRevision": {"id": "q2"},
            "questionIndex": 1,
            "reviewType": "Downward",
            "reviewer": {"id": "secret-manager-id"},
            "response": {"preciseRating": 4.0, "comment": "Strong"},
        },
        {
            "id": "r1",
            "questionRevision": {"id": "q1"},
            "questionIndex": 0,
            "reviewType": "Peer",
            "reviewer": {"id": "secret-peer-id"},
            "response": {"comment": "Shipped the migration", "choices": ["Delivery"]},
            "calibratedResponse": {"comment": "Shipped it"},
        },
    ]
    text = reviewee_content(reviews, questions)

    assert text.splitlines() == [
        "Question: What went well?",
        "- Peer review: chose Delivery; comment: Shipped the migration",
        "- Peer review, calibrated: comment: Shipped it",
        "",
        "Question: Rate collaboration",
        "- Downward review: rating 4; comment: Strong",
    ]
    assert "secret" not in text


def test_a_review_with_an_unreadable_question_falls_back_to_its_id():
    text = reviewee_content(
        [{"id": "r", "questionRevision": {"id": "q9"}, "response": {"comment": "ok"}}], {}
    )
    assert text.startswith("Question: q9")
