"""Unit tests for the Lever connector.

The Lever API is fully mocked via ``FakeLeverSession`` (see conftest.py) — no
network traffic and no live API key, so these run in CI. Coverage:

  - HTML list content and form-field values render to readable text
  - first sync yields every in-scope posting and records the cursor
  - incremental sync pushes ``updated_at_start`` down and yields only the delta
  - /postings/deleted ids become hard-delete tombstones (30-day windows)
  - postings that leave the selected scope are forgotten
  - confidential postings/opportunities are skipped by default
  - feedback + notes are opt-in and never carry candidate contact data
  - deleted opportunities / emptied opportunities become tombstones
  - scoped rescan (posting_ids) catches feedback added without an updatedAt
    change, forgets opportunities that leave the scope, and never mass-forgets
    on an empty listing
  - candidate names/emails/phones in free text become a stable pseudonym
  - anonymized (GDPR-erased) candidates are forgotten and never re-read
  - pagination, retry on 429, and "errors never advance the cursor"
  - the dlt resource is wired for merge + hard delete + document mode
  - a real dlt merge physically removes a tombstoned row (forget-on-delete)
"""

import dlt
import pytest
from conftest import (
    DAY_MS,
    FakeLeverSession,
    FakeResponse,
    feedback,
    note,
    opportunity,
    posting,
)

from cognee_community_connector_lever import lever_source
from cognee_community_connector_lever.lever import (
    _CURSOR_OVERLAP_MS,
    LEVER_SOURCE_NAME,
    LEVER_TABLE_NAME,
    _candidate_label,
    _format_value,
    _html_to_text,
    _posting_to_row,
    sync_opportunities,
    sync_postings,
)

NOW = 1_800_000_000_000
LATER = NOW + DAY_MS


def _ids(rows, deleted=False):
    return sorted(r["id"] for r in rows if bool(r.get("_deleted")) is deleted)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_html_to_text_turns_list_items_into_bullets():
    raw = "<ul><li>Python &amp; SQL</li><li><b>Kafka</b></li></ul><p>Nice to have</p>"
    assert _html_to_text(raw) == "- Python & SQL\n- Kafka\nNice to have"
    assert _html_to_text(None) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("  yes  ", "yes"),
        (True, "yes"),
        (4, "4"),
        (["Python", "", "Go"], "Python, Go"),
        ({"skill": "Design", "score": 3}, "skill: Design; score: 3"),
    ],
)
def test_format_value_handles_every_field_shape(value, expected):
    assert _format_value(value) == expected


def test_posting_row_is_a_document_with_facts_lists_and_url():
    row = _posting_to_row(posting("p1", updated_at=NOW))
    assert row["id"] == "posting:p1"
    assert row["title"] == "Backend Engineer"
    assert row["url"] == "https://jobs.lever.co/acme/p1"
    assert row["_deleted"] is False
    content = row["content"]
    assert "Team: Platform" in content
    assert "Location: Bengaluru" in content
    assert "## Requirements\n- Python\n- SQL" in content
    assert "Build APIs." in content
    # Only document columns: metadata-only edits must not churn the content hash.
    assert set(row) == {"id", "title", "content", "url", "_deleted"}


# ---------------------------------------------------------------------------
# Postings sync
# ---------------------------------------------------------------------------
def test_first_sync_yields_all_postings_and_records_cursor():
    session = FakeLeverSession(postings=[posting("p1", updated_at=1), posting("p2", updated_at=2)])
    state = {}

    rows = list(sync_postings(session, state, now_ms=NOW))

    assert _ids(rows) == ["posting:p1", "posting:p2"]
    assert _ids(rows, deleted=True) == []
    assert state["postings_cursor"] == NOW
    # No delete feed on the first run (nothing has been ingested yet).
    assert "/postings/deleted" not in session.paths()
    assert "updated_at_start" not in session.calls[0][1]


def test_incremental_sync_pushes_cursor_down_and_yields_only_changes():
    session = FakeLeverSession(
        postings=[
            posting("old", updated_at=NOW - 10 * DAY_MS),
            posting("new", updated_at=NOW + 1000),
        ]
    )
    state = {"postings_cursor": NOW}

    rows = list(sync_postings(session, state, now_ms=LATER))

    assert _ids(rows) == ["posting:new"]
    assert session.calls[0] == (
        "/postings",
        {"updated_at_start": NOW - _CURSOR_OVERLAP_MS, "limit": 100},
    )
    assert state["postings_cursor"] == LATER


def test_deleted_postings_become_tombstones_in_30_day_windows():
    cursor = NOW - 70 * DAY_MS
    session = FakeLeverSession(
        deleted_postings=[
            {"id": "gone1", "deletedAt": NOW - 60 * DAY_MS},
            {"id": "gone2", "deletedAt": NOW - DAY_MS},
        ]
    )
    state = {"postings_cursor": cursor}

    rows = list(sync_postings(session, state, now_ms=NOW))

    assert _ids(rows, deleted=True) == ["posting:gone1", "posting:gone2"]
    windows = [p for path, p in session.calls if path == "/postings/deleted"]
    assert len(windows) == 3  # 70 days split into <=29-day windows
    for window in windows:
        assert window["deleted_at_end"] - window["deleted_at_start"] <= 29 * DAY_MS
    assert windows[0]["deleted_at_start"] == cursor - _CURSOR_OVERLAP_MS
    assert windows[-1]["deleted_at_end"] == NOW


def test_posting_leaving_selected_states_is_forgotten():
    session = FakeLeverSession(
        postings=[
            posting("open", updated_at=NOW + 1, state="published"),
            posting("filled", updated_at=NOW + 1, state="closed"),
        ]
    )

    # First run: out-of-scope postings are simply not ingested.
    first = list(sync_postings(session, {}, now_ms=NOW, posting_states=["published"]))
    assert _ids(first) == ["posting:open"]
    assert _ids(first, deleted=True) == []

    # Later run: a posting that moved out of scope is tombstoned.
    later = list(
        sync_postings(session, {"postings_cursor": NOW}, now_ms=LATER, posting_states=["published"])
    )
    assert _ids(later) == ["posting:open"]
    assert _ids(later, deleted=True) == ["posting:filled"]


def test_confidential_postings_are_skipped_unless_opted_in():
    session = FakeLeverSession(
        postings=[
            posting("public", updated_at=1),
            posting("secret", updated_at=1, confidentiality="confidential"),
        ]
    )
    assert _ids(list(sync_postings(session, {}, now_ms=NOW))) == ["posting:public"]
    assert _ids(list(sync_postings(session, {}, now_ms=NOW, include_confidential=True))) == [
        "posting:public",
        "posting:secret",
    ]


def test_pagination_follows_the_next_token():
    session = FakeLeverSession(
        postings=[posting(f"p{i}", updated_at=i) for i in range(5)], page_size=2
    )
    rows = list(sync_postings(session, {}, now_ms=NOW))
    assert len(_ids(rows)) == 5
    offsets = [p.get("offset") for path, p in session.calls if path == "/postings"]
    assert offsets == [None, "2", "4"]


def test_full_refresh_relists_everything_but_still_reads_the_delete_feed():
    session = FakeLeverSession(
        postings=[posting("p1", updated_at=1)],
        deleted_postings=[{"id": "gone", "deletedAt": NOW + 1}],
    )
    rows = list(sync_postings(session, {"postings_cursor": NOW}, now_ms=LATER, full_refresh=True))
    assert _ids(rows) == ["posting:p1"]
    assert _ids(rows, deleted=True) == ["posting:gone"]
    assert "updated_at_start" not in session.calls[0][1]


def test_rate_limit_is_retried():
    session = FakeLeverSession(postings=[posting("p1", updated_at=1)])
    session.injected = [FakeResponse(status_code=429, headers={"Retry-After": "0"})]
    assert _ids(list(sync_postings(session, {}, now_ms=NOW))) == ["posting:p1"]
    assert session.paths() == ["/postings", "/postings"]


def test_permanent_error_propagates_and_does_not_advance_the_cursor():
    session = FakeLeverSession(postings=[posting("p1", updated_at=1)])
    session.injected = [FakeResponse(status_code=401)]
    state = {"postings_cursor": NOW}
    with pytest.raises(RuntimeError, match="401"):
        list(sync_postings(session, state, now_ms=LATER))
    assert state == {"postings_cursor": NOW}


# ---------------------------------------------------------------------------
# Feedback + notes (restricted candidate data)
# ---------------------------------------------------------------------------
def _candidate_session(**overrides):
    kwargs = {
        "opportunities": [opportunity("o1", updated_at=NOW + 1, posting="p1")],
        "feedback_by_opp": {
            "o1": [feedback("f1"), feedback("f-deleted", deleted_at=NOW)],
        },
        "notes_by_opp": {
            "o1": [
                note("n1", "Strong on distributed systems."),
                note("n-secret", "Compensation expectations: ...", secret=True),
            ]
        },
    }
    kwargs.update(overrides)
    return FakeLeverSession(**kwargs)


def test_feedback_and_notes_render_without_candidate_contact_data():
    session = _candidate_session()
    rows = list(
        sync_opportunities(session, {}, now_ms=NOW, include_feedback=True, include_notes=True)
    )

    assert _ids(rows) == ["opportunity:o1"]
    row = rows[0]
    content = row["content"]
    assert "## Feedback: On-site interview" in content
    assert "- Rating: 4 - Strong Hire" in content
    assert "Strong on distributed systems." in content
    # Deleted and secret forms are skipped.
    assert content.count("## Feedback") == 1
    assert "Compensation" not in content
    # Restricted by default: no candidate contact data anywhere in the row.
    flat = repr(row)
    for pii in ("Jane Candidate", "jane@example.com", "555 0100", "Initech", "Springfield"):
        assert pii not in flat
    assert row["url"] == "https://hire.lever.co/candidates/o1"


def test_only_the_selected_kinds_are_fetched():
    session = _candidate_session()
    rows = list(
        sync_opportunities(session, {}, now_ms=NOW, include_feedback=True, include_notes=False)
    )
    assert "Strong on distributed systems." not in rows[0]["content"]
    assert not any(path.endswith("/notes") for path in session.paths())


def test_posting_ids_are_pushed_down_as_a_repeated_param():
    session = _candidate_session()
    list(
        sync_opportunities(
            session,
            {},
            now_ms=NOW,
            include_feedback=True,
            include_notes=False,
            posting_ids=["p1", "p2"],
        )
    )
    assert session.calls[0] == ("/opportunities", {"posting_id": ["p1", "p2"], "limit": 100})


def test_deleted_emptied_and_confidential_opportunities_are_forgotten():
    session = _candidate_session(
        opportunities=[
            opportunity("o1", updated_at=NOW + 1),
            opportunity("o-empty", updated_at=NOW + 1),
            opportunity("o-conf", updated_at=NOW + 1, confidentiality="confidential"),
        ],
        feedback_by_opp={"o1": [feedback("f1")], "o-conf": [feedback("f2")]},
        notes_by_opp={},
        deleted_opportunities=[{"id": "o-gone", "deletedAt": NOW + 5}],
    )
    state = {"opportunities_cursor": NOW}

    rows = list(
        sync_opportunities(session, state, now_ms=LATER, include_feedback=True, include_notes=True)
    )

    assert _ids(rows) == ["opportunity:o1"]
    assert _ids(rows, deleted=True) == [
        "opportunity:o-conf",
        "opportunity:o-empty",
        "opportunity:o-gone",
    ]
    # Confidential opportunities are never even read.
    assert "/opportunities/o-conf/feedback" not in session.paths()
    assert state["opportunities_cursor"] == LATER


# ---------------------------------------------------------------------------
# Scoped rescan: feedback that does not bump the opportunity's updatedAt
# ---------------------------------------------------------------------------
def _sync_scoped(session, state, now_ms, **kwargs):
    return list(
        sync_opportunities(
            session,
            state,
            now_ms=now_ms,
            include_feedback=True,
            include_notes=True,
            posting_ids=["p1"],
            **kwargs,
        )
    )


def test_scoped_rescan_picks_up_feedback_added_without_an_updatedat_change():
    # The opportunity was last updated long before both runs.
    session = _candidate_session(
        opportunities=[opportunity("o1", updated_at=NOW - 30 * DAY_MS, posting="p1")],
        feedback_by_opp={"o1": [feedback("f1")]},
        notes_by_opp={},
    )
    state = {}
    first = _sync_scoped(session, state, NOW)
    assert _ids(first) == ["opportunity:o1"]

    # New feedback lands; Lever does NOT move the opportunity's updatedAt.
    session.feedback_by_opp["o1"].append(
        feedback(
            "f2",
            text="Hiring manager screen",
            fields=[{"text": "Notes", "value": "Strong ownership mindset."}],
        )
    )
    session.calls.clear()
    second = _sync_scoped(session, state, LATER)

    assert _ids(second) == ["opportunity:o1"]
    assert "Strong ownership mindset." in second[0]["content"]
    # The scoped listing is a full rescan, never filtered by updatedAt.
    assert "updated_at_start" not in session.calls[0][1]
    assert state["opportunity_ids"] == ["o1"]


def test_scoped_rescan_of_unchanged_feedback_yields_identical_rows():
    """Identical rows keep their content-hash data_id, so nothing is re-cognified."""
    session = _candidate_session(
        opportunities=[opportunity("o1", updated_at=NOW - DAY_MS, posting="p1")]
    )
    state = {}
    first = _sync_scoped(session, state, NOW)
    second = _sync_scoped(session, state, LATER)
    assert first == second


def test_scoped_rescan_forgets_opportunities_that_leave_the_scope():
    session = _candidate_session(
        opportunities=[
            opportunity("o1", updated_at=NOW - DAY_MS, posting="p1"),
            opportunity("o2", updated_at=NOW - DAY_MS, posting="p1"),
        ],
        feedback_by_opp={"o1": [feedback("f1")], "o2": [feedback("f2")]},
        notes_by_opp={},
    )
    state = {}
    assert _ids(_sync_scoped(session, state, NOW)) == ["opportunity:o1", "opportunity:o2"]

    # o2 is no longer on posting p1 (e.g. moved to another role).
    session.opportunities[1]["applications"] = ["p9"]
    rows = _sync_scoped(session, state, LATER)

    assert _ids(rows) == ["opportunity:o1"]
    assert _ids(rows, deleted=True) == ["opportunity:o2"]
    assert state["opportunity_ids"] == ["o1"]


def test_scoped_rescan_never_mass_forgets_on_an_empty_listing():
    session = _candidate_session(opportunities=[], feedback_by_opp={}, notes_by_opp={})
    state = {"opportunities_cursor": NOW, "opportunity_ids": ["o1", "o2"]}

    rows = _sync_scoped(session, state, LATER)

    assert rows == []
    # The known scope is preserved, so a later healthy run still reconciles it.
    assert state["opportunity_ids"] == ["o1", "o2"]
    assert state["opportunities_cursor"] == LATER


def test_each_id_is_tombstoned_at_most_once_per_run():
    # o-gone left the scope AND appears in the delete feed.
    session = _candidate_session(
        opportunities=[opportunity("o1", updated_at=NOW - DAY_MS, posting="p1")],
        deleted_opportunities=[{"id": "o-gone", "deletedAt": NOW + 5}],
    )
    state = {"opportunities_cursor": NOW, "opportunity_ids": ["o1", "o-gone"]}

    rows = _sync_scoped(session, state, LATER)

    tombstones = [r["id"] for r in rows if r.get("_deleted")]
    assert tombstones == ["opportunity:o-gone"]


def test_account_wide_mode_stays_incremental_and_drops_scoped_state():
    session = _candidate_session()
    state = {"opportunities_cursor": NOW, "opportunity_ids": ["o1"]}

    list(
        sync_opportunities(session, state, now_ms=LATER, include_feedback=True, include_notes=True)
    )

    assert session.calls[0][1]["updated_at_start"] == NOW - _CURSOR_OVERLAP_MS
    assert "opportunity_ids" not in state


# ---------------------------------------------------------------------------
# Candidate pseudonymization and anonymization
# ---------------------------------------------------------------------------
def test_candidate_identifiers_in_free_text_become_a_stable_pseudonym():
    session = _candidate_session(
        opportunities=[
            opportunity("o1", updated_at=NOW + 1, name="Jane Q. Doe", contact="contact-1")
        ],
        feedback_by_opp={
            "o1": [
                feedback(
                    "f1",
                    fields=[
                        {
                            "text": "Notes",
                            "value": (
                                "JANE was great; jane's design was clean. Ms. Doe asked "
                                "about Janet's team. Reach her at Jane@Example.com or "
                                "+1 555 0100. Jane Q. Doe is a strong hire."
                            ),
                        }
                    ],
                )
            ]
        },
        notes_by_opp={},
    )

    (row,) = list(
        sync_opportunities(session, {}, now_ms=NOW, include_feedback=True, include_notes=False)
    )

    label = _candidate_label({"id": "o1", "contact": "contact-1"})
    content = row["content"]
    assert label in content
    assert label in row["title"]
    lowered = content.lower()
    for leaked in ("jane was", "jane's", "doe", "jane@example.com", "555 0100"):
        assert leaked not in lowered, leaked
    # Whole-word matching: a different person's name is left intact.
    assert "Janet's team" in content
    assert f"{label}'s design" in content
    assert f"{label} is a strong hire" in content
    assert "Jane" not in row["title"]


def test_candidate_label_is_stable_per_person_and_distinct_across_people():
    same_person_a = _candidate_label({"id": "o1", "contact": "c1"})
    same_person_b = _candidate_label({"id": "o2", "contact": "c1"})
    other_person = _candidate_label({"id": "o3", "contact": "c2"})
    no_contact = _candidate_label({"id": "o4", "contact": None})

    assert same_person_a == same_person_b
    assert same_person_a != other_person
    assert no_contact.startswith("Candidate ")
    # One-way: the label never contains the underlying id.
    assert "c1" not in same_person_a.removeprefix("Candidate ")


def test_anonymized_candidates_are_forgotten_and_never_read():
    session = _candidate_session(
        opportunities=[opportunity("o1", updated_at=NOW + 1, anonymized=True)]
    )

    # First run: nothing was ingested, so nothing is emitted at all.
    assert (
        list(sync_opportunities(session, {}, now_ms=NOW, include_feedback=True, include_notes=True))
        == []
    )

    # Later run: whatever was ingested for this person is tombstoned.
    rows = list(
        sync_opportunities(
            session,
            {"opportunities_cursor": NOW},
            now_ms=LATER,
            include_feedback=True,
            include_notes=True,
        )
    )
    assert _ids(rows, deleted=True) == ["opportunity:o1"]
    assert not any(p.startswith("/opportunities/o1/") for p in session.paths())


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_resource_is_configured_for_merge_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    resource = lever_source(session=FakeLeverSession())
    assert resource.name == LEVER_TABLE_NAME

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == LEVER_SOURCE_NAME


def test_lever_source_validates_its_arguments(monkeypatch):
    monkeypatch.delenv("LEVER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        lever_source()
    with pytest.raises(ValueError, match="Nothing to ingest"):
        lever_source(session=FakeLeverSession(), include_postings=False)
    # The env var is enough.
    monkeypatch.setenv("LEVER_API_KEY", "key")
    assert lever_source() is not None


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    """Sync twice through a real dlt pipeline: the tombstone removes the row."""
    pipeline = dlt.pipeline(
        pipeline_name="test_lever_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'lever.db'}"),
        dataset_name="lever",
    )
    clock = iter([NOW, LATER])

    def run(session):
        pipeline.run(lever_source(session=session, clock=lambda: next(clock)))
        with pipeline.sql_client() as client:
            return client.execute_sql(f"SELECT id, title FROM {LEVER_TABLE_NAME} ORDER BY id")

    # Sync #1: two postings land in the destination.
    first = FakeLeverSession(
        postings=[
            posting("p1", updated_at=NOW - 5, text="Backend Engineer"),
            posting("p2", updated_at=NOW - 5, text="Data Scientist"),
        ]
    )
    assert run(first) == [("posting:p1", "Backend Engineer"), ("posting:p2", "Data Scientist")]

    # Sync #2: p1 edited, p2 deleted upstream. Only the delta is read; the
    # tombstone for p2 hard-deletes it and p1 is upserted in place.
    second = FakeLeverSession(
        postings=[posting("p1", updated_at=NOW + 10, text="Senior Backend Engineer")],
        deleted_postings=[{"id": "p2", "deletedAt": NOW + 20}],
    )
    assert run(second) == [("posting:p1", "Senior Backend Engineer")]
    assert second.calls[0][1]["updated_at_start"] == NOW - _CURSOR_OVERLAP_MS


def test_scoped_feedback_sync_end_to_end_through_a_real_dlt_merge(tmp_path):
    """New feedback is upserted and an out-of-scope opportunity is removed."""
    pipeline = dlt.pipeline(
        pipeline_name="test_lever_feedback_e2e",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'lever.db'}"),
        dataset_name="lever",
    )
    session = FakeLeverSession(
        opportunities=[
            opportunity("o1", updated_at=NOW - 30 * DAY_MS, posting="p1"),
            opportunity("o2", updated_at=NOW - 30 * DAY_MS, posting="p1"),
        ],
        feedback_by_opp={"o1": [feedback("f1")], "o2": [feedback("f2")]},
    )
    clock = iter([NOW, LATER])

    def run():
        pipeline.run(
            lever_source(
                session=session,
                include_postings=False,
                include_feedback=True,
                posting_ids=["p1"],
                clock=lambda: next(clock),
            )
        )
        with pipeline.sql_client() as client:
            rows = client.execute_sql(f"SELECT id, content FROM {LEVER_TABLE_NAME} ORDER BY id")
        return dict(rows)

    assert set(run()) == {"opportunity:o1", "opportunity:o2"}

    # Feedback added to o1 (its updatedAt does not move); o2 leaves the posting.
    session.feedback_by_opp["o1"].append(
        feedback("f3", fields=[{"text": "Notes", "value": "Excellent debugging."}])
    )
    session.opportunities[1]["applications"] = ["p9"]
    final = run()

    assert set(final) == {"opportunity:o1"}
    assert "Excellent debugging." in final["opportunity:o1"]
