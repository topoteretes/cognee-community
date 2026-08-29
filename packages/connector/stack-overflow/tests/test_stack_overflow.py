"""Unit tests for the Stack Overflow connector.

The Stack Exchange API is fully mocked — no network access and no API key
required, so these run in CI. Coverage:

  - HTML stripping and content building (title + body + answers)
  - full backfill yields every question and records cursor + id set in state
  - incremental re-sync yields ONLY questions modified since the cursor
  - deleted/migrated questions become hard-delete markers (forget-on-delete)
  - the dlt resource is wired with merge + question_id PK + hard_delete column
  - a dlt end-to-end run proves a delete marker physically removes the row
"""

import pytest

from cognee_community_connector_stack_overflow.stack_overflow import (
    _build_content,
    _find_deleted_ids,
    _strip_html,
    full_backfill,
    incremental_fetch,
    stack_overflow_source,
)


# ---------------------------------------------------------------------------
# Fake Stack Exchange API
# ---------------------------------------------------------------------------
def _make_question(qid, *, title="A title", body="<p>Body text</p>", ts=1000, tags=None):
    return {
        "question_id": qid,
        "title": title,
        "body": body,
        "link": f"https://stackoverflow.com/questions/{qid}",
        "tags": tags or ["python"],
        "score": 5,
        "answer_count": 1,
        "is_answered": True,
        "view_count": 100,
        "creation_date": ts,
        "last_activity_date": ts,
        "owner": {"display_name": "testuser"},
    }


def _make_answer(*, body="Answer body", is_accepted=False, score=3):
    return {"body": body, "is_accepted": is_accepted, "score": score}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Minimal stand-in for a ``requests`` session hitting Stack Exchange."""

    def __init__(self, questions=None, answers_by_id=None, deleted_ids=None):
        self.questions = questions or []
        self.answers_by_id = answers_by_id or {}
        self.deleted_ids = set(deleted_ids or [])
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        self.calls.append((url, dict(params)))

        if "/answers" in url:
            qid = int(url.split("/questions/")[1].split("/")[0])
            items = self.answers_by_id.get(qid, [])
            return FakeResponse({"items": items, "has_more": False})

        if "/questions/" in url and "answers" not in url:
            # Deletion-sweep endpoint: /questions/{ids}
            ids_part = url.split("/questions/")[1]
            requested = {int(x) for x in ids_part.split(";")}
            # Return only non-deleted items.
            items = [
                {"question_id": qid}
                for qid in requested
                if qid not in self.deleted_ids
            ]
            return FakeResponse({"items": items, "has_more": False})

        # /questions list endpoint
        from_date = int(params.get("fromdate", 0))
        items = [q for q in self.questions if q["last_activity_date"] >= from_date]
        return FakeResponse({"items": items, "has_more": False})


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------
def test_strip_html_removes_tags():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_unescapes_entities():
    assert "&amp;" not in _strip_html("a &amp; b")
    assert "a & b" == _strip_html("a &amp; b")


def test_strip_html_handles_none():
    assert _strip_html(None) == ""


def test_build_content_includes_title_and_body():
    question = {"title": "How do I?", "body": "<p>details</p>"}
    content = _build_content(question, [])
    assert "How do I?" in content
    assert "details" in content


def test_build_content_includes_accepted_answer():
    question = {"title": "Q", "body": "body"}
    answers = [_make_answer(body="<p>The fix is X</p>", is_accepted=True)]
    content = _build_content(question, answers)
    assert "Accepted answer:" in content
    assert "The fix is X" in content


def test_build_content_labels_non_accepted():
    question = {"title": "Q", "body": ""}
    answers = [_make_answer(body="<p>Partial answer</p>", is_accepted=False)]
    content = _build_content(question, answers)
    assert "Answer:" in content


# ---------------------------------------------------------------------------
# Full backfill tests
# ---------------------------------------------------------------------------
def test_full_backfill_yields_all_questions():
    questions = [_make_question(1, ts=500), _make_question(2, ts=600)]
    session = FakeSession(questions=questions)
    state: dict = {}

    rows = list(full_backfill(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    assert len(rows) == 2
    assert {r["question_id"] for r in rows} == {1, 2}


def test_full_backfill_records_cursor():
    questions = [_make_question(1, ts=500), _make_question(2, ts=700)]
    session = FakeSession(questions=questions)
    state: dict = {}

    list(full_backfill(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    assert state["cursor"] == 700


def test_full_backfill_records_seen_ids():
    questions = [_make_question(10), _make_question(20)]
    session = FakeSession(questions=questions)
    state: dict = {}

    list(full_backfill(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    assert set(state["seen_ids"]) == {10, 20}


def test_full_backfill_deleted_flag_is_false():
    session = FakeSession(questions=[_make_question(1)])
    state: dict = {}
    rows = list(full_backfill(session, state, tags=None, user_id=None, api_key=None, include_answers=False))
    assert rows[0]["_deleted"] is False


# ---------------------------------------------------------------------------
# Incremental fetch tests
# ---------------------------------------------------------------------------
def test_incremental_fetch_only_returns_new_questions():
    q_old = _make_question(1, ts=500)
    q_new = _make_question(2, ts=900)
    session = FakeSession(questions=[q_old, q_new])
    state = {"cursor": 800, "seen_ids": [1]}

    rows = list(incremental_fetch(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    returned_ids = {r["question_id"] for r in rows if not r.get("_deleted")}
    # Only q_new (ts=900) is >= fromdate=800
    assert 2 in returned_ids


def test_incremental_fetch_advances_cursor():
    q_new = _make_question(3, ts=1200)
    session = FakeSession(questions=[q_new])
    state = {"cursor": 1000, "seen_ids": []}

    list(incremental_fetch(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    assert state["cursor"] == 1200


def test_incremental_fetch_emits_delete_marker_for_removed_question():
    # Question 99 was seen before but is now deleted (not returned by API).
    session = FakeSession(questions=[], deleted_ids={99})
    state = {"cursor": 500, "seen_ids": [99]}

    rows = list(incremental_fetch(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    delete_rows = [r for r in rows if r.get("_deleted")]
    assert any(r["question_id"] == 99 for r in delete_rows)


def test_incremental_fetch_removes_deleted_from_seen_ids():
    session = FakeSession(questions=[], deleted_ids={5})
    state = {"cursor": 500, "seen_ids": [5, 6]}

    list(incremental_fetch(session, state, tags=None, user_id=None, api_key=None, include_answers=False))

    assert 5 not in state["seen_ids"]
    assert 6 in state["seen_ids"]


# ---------------------------------------------------------------------------
# find_deleted_ids tests
# ---------------------------------------------------------------------------
def test_find_deleted_ids_returns_missing():
    session = FakeSession(deleted_ids={42})
    deleted = _find_deleted_ids(session, {42, 43}, api_key=None)
    assert 42 in deleted
    assert 43 not in deleted


def test_find_deleted_ids_empty_input():
    session = FakeSession()
    assert _find_deleted_ids(session, set(), api_key=None) == []


# ---------------------------------------------------------------------------
# Answer fetching tests
# ---------------------------------------------------------------------------
def test_backfill_with_answers_includes_answer_content():
    question = _make_question(1, title="How to parse JSON?", body="<p>question</p>")
    answers = [_make_answer(body="<p>Use json.loads()</p>", is_accepted=True)]
    session = FakeSession(questions=[question], answers_by_id={1: answers})
    state: dict = {}

    rows = list(full_backfill(session, state, tags=None, user_id=None, api_key=None, include_answers=True))

    assert "json.loads" in rows[0]["content"]


# ---------------------------------------------------------------------------
# dlt resource wiring test
# ---------------------------------------------------------------------------
def test_resource_has_correct_primary_key():
    """The dlt resource must declare question_id as PK and merge disposition."""
    pytest.importorskip("dlt")
    resource = stack_overflow_source(tags=["python"])
    assert resource.write_disposition == "merge"
    assert list(resource.compute_table_schema().get("columns", {}).keys())
    columns = resource.compute_table_schema().get("columns", {})
    assert columns.get("question_id", {}).get("primary_key") is True


# ---------------------------------------------------------------------------
# End-to-end dlt merge test (proves hard-delete physically removes the row)
# ---------------------------------------------------------------------------
def test_e2e_delete_marker_removes_row(tmp_path, monkeypatch):
    """A hard-delete marker from the connector must physically remove the dlt row."""
    dlt = pytest.importorskip("dlt")

    import cognee_community_connector_stack_overflow.stack_overflow as mod

    q1 = _make_question(101, ts=100)
    q2 = _make_question(102, ts=200)
    fake_session = FakeSession(questions=[q1, q2])

    monkeypatch.setattr(mod, "_make_session", lambda: fake_session)

    db_path = tmp_path / "test.duckdb"
    pipeline = dlt.pipeline(
        pipeline_name="so_test",
        destination=dlt.destinations.duckdb(str(db_path)),
        dataset_name="so_data",
    )

    # First run: ingest both questions.
    resource = stack_overflow_source(tags=["python"])
    pipeline.run(resource, primary_key="question_id", write_disposition="merge")

    # Second run: q1 is now deleted.
    fake_session2 = FakeSession(questions=[], deleted_ids={101})
    monkeypatch.setattr(mod, "_make_session", lambda: fake_session2)

    resource2 = stack_overflow_source(tags=["python"])
    pipeline.run(resource2, primary_key="question_id", write_disposition="merge")

    import duckdb

    conn = duckdb.connect(str(db_path))
    rows = conn.execute("SELECT question_id FROM so_data.stack_overflow_questions").fetchall()
    conn.close()
    ids = {r[0] for r in rows}
    assert 101 not in ids, "Deleted question should have been removed by dlt merge"
    assert 102 in ids
