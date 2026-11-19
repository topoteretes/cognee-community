"""Unit tests for the Stack Overflow connector.

The Stack Exchange API is fully mocked via ``FakeStackExchangeSession`` — no
``requests`` traffic and no live key are required, so these run in CI.
Coverage:

  - HTML bodies are converted to plain text (code fences / paragraphs preserved)
  - answers are ordered accepted-first, then by score, and truncated
  - the incremental cursor reads last_activity_date and skips unchanged questions
  - full sync yields every question and records the cursor + id set
  - questions that vanish from the sweep become hard-delete markers (forget-on-delete)
  - a question deleted between the sweep and the detail fetch is still forgotten
  - an empty sweep does not mass-delete a previously known corpus
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a real dlt merge removes the marked row (end-to-end forget-on-delete)

The end-to-end "deletion removes from memory" guarantee is provided by cognee's
existing ``orphan_cleanup`` path; here we prove the connector emits the markers
that drive it, and that dlt acts on them.
"""

import pytest

from cognee_community_connector_stack_overflow.stack_overflow import (
    STACK_OVERFLOW_SOURCE_NAME,
    _clean_html,
    _deleted_row,
    _question_to_row,
    _select_answers,
    stack_overflow_source,
    sync_questions,
)

SITE = "stackoverflow"


# ---------------------------------------------------------------------------
# Fake Stack Exchange API
# ---------------------------------------------------------------------------
def _summary(question_id, *, when, tags=None, title="A question"):
    """A question as the API returns it from a plain /questions listing (no body)."""
    return {
        "question_id": question_id,
        "title": title,
        "tags": tags or ["python"],
        "last_activity_date": when,
        "link": f"https://stackoverflow.com/questions/{question_id}",
    }


def _detail(question_id, *, body="", accepted_answer_id=None, **extra):
    """A question as returned with filter=withbody."""
    row = _summary(question_id, when=extra.pop("when", 0), **extra)
    row["body"] = body
    if accepted_answer_id is not None:
        row["accepted_answer_id"] = accepted_answer_id
    return row


def _answer(answer_id, question_id, *, score=0, body="", accepted=False):
    return {
        "answer_id": answer_id,
        "question_id": question_id,
        "score": score,
        "body": body,
        "is_accepted": accepted,
    }


class FakeStackExchangeSession:
    """Minimal stand-in for a ``requests`` session hitting the SE API."""

    def __init__(self, summaries, details_by_id=None, answers_by_question=None):
        self.summaries = summaries
        self.details_by_id = details_by_id or {}
        self.answers_by_question = answers_by_question or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, dict(params)))

        if url.endswith("/questions"):
            return _Resp({"items": self.summaries, "has_more": False})

        if "/answers" in url:
            ids = [int(i) for i in url.split("/questions/")[1].split("/answers")[0].split(";")]
            items = [a for qid in ids for a in self.answers_by_question.get(qid, [])]
            return _Resp({"items": items, "has_more": False})

        if "/questions/" in url:
            ids = [int(i) for i in url.split("/questions/")[1].split(";")]
            items = [self.details_by_id[i] for i in ids if i in self.details_by_id]
            return _Resp({"items": items, "has_more": False})

        raise AssertionError(f"unexpected URL: {url}")


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _session(summaries, details=None, answers=None):
    details_by_id = {d["question_id"]: d for d in (details or [])}
    return FakeStackExchangeSession(summaries, details_by_id, answers or {})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_clean_html_strips_tags_preserves_paragraphs_and_code():
    raw = "<p>Hello&nbsp;<b>world</b></p><pre><code>x = 1\ny = 2</code></pre>"
    cleaned = _clean_html(raw)
    assert "Hello world" in cleaned
    assert "```" in cleaned
    assert "x = 1\ny = 2" in cleaned
    assert _clean_html("") == ""
    assert _clean_html(None) == ""


def test_select_answers_orders_accepted_first_then_by_score_and_truncates():
    answers = [
        _answer(1, 100, score=5),
        _answer(2, 100, score=20),
        _answer(3, 100, score=1),
    ]
    ordered = _select_answers(answers, accepted_id=1, limit=2)
    assert [a["answer_id"] for a in ordered] == [1, 2]  # accepted first, then highest score


def test_select_answers_handles_no_accepted_and_empty():
    answers = [_answer(1, 100, score=5), _answer(2, 100, score=20)]
    ordered = _select_answers(answers, accepted_id=None, limit=10)
    assert [a["answer_id"] for a in ordered] == [2, 1]
    assert _select_answers([], accepted_id=None, limit=3) == []


def test_question_to_row_folds_answers_into_content():
    question = _detail(100, body="<p>How do I do X?</p>", accepted_answer_id=2, tags=["python"])
    answers = [
        _answer(2, 100, score=10, body="<p>Do Y.</p>", accepted=True),
        _answer(3, 100, score=1, body="<p>Or Z.</p>"),
    ]
    row = _question_to_row(question, answers)

    assert row["id"] == "100"
    assert row["title"] == "A question"
    assert "Tags: python" in row["content"]
    assert "How do I do X?" in row["content"]
    assert "Accepted Answer" in row["content"]
    assert "Do Y." in row["content"]
    assert "Or Z." in row["content"]
    assert row["url"] == "https://stackoverflow.com/questions/100"
    assert row["_deleted"] is False


def test_question_to_row_with_no_answers():
    question = _detail(100, body="<p>Body</p>")
    row = _question_to_row(question, [])
    assert "## Answers" not in row["content"]


def test_deleted_row_shape():
    assert _deleted_row("42") == {"id": "42", "_deleted": True}


# ---------------------------------------------------------------------------
# sync_questions — backfill / incremental / deletion
# ---------------------------------------------------------------------------
def test_first_sync_yields_all_questions_and_records_cursor_and_ids():
    session = _session(
        summaries=[_summary(1, when=100), _summary(2, when=200)],
        details=[_detail(1, when=100, body="<p>a</p>"), _detail(2, when=200, body="<p>b</p>")],
    )
    state: dict = {}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    assert {r["id"] for r in rows} == {"1", "2"}
    assert all(r["_deleted"] is False for r in rows)
    assert state["last_when"] == 200
    assert state["known_ids"] == ["1", "2"]


def test_incremental_yields_only_questions_changed_since_cursor():
    session = _session(
        summaries=[_summary(1, when=100), _summary(2, when=200)],
        details=[_detail(2, when=200, body="<p>new</p>")],
    )
    state = {"known_ids": ["1", "2"], "last_when": 150}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    assert [r["id"] for r in rows] == ["2"]
    assert state["last_when"] == 200


def test_incremental_no_changes_is_a_noop():
    session = _session(summaries=[_summary(1, when=100)])
    state = {"known_ids": ["1"], "last_when": 100}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )
    assert rows == []


def test_deleted_question_emits_hard_delete_marker():
    # Question "2" was known last run but is gone from the sweep now.
    session = _session(summaries=[_summary(1, when=100)])
    state = {"known_ids": ["1", "2"], "last_when": 100}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    assert rows == [{"id": "2", "_deleted": True}]
    assert state["known_ids"] == ["1"]


def test_question_vanished_between_sweep_and_detail_fetch_is_forgotten():
    # The sweep lists question "2" as live, but the detail fetch (a moment
    # later) can no longer find it — treat as deleted rather than dropping it
    # silently.
    session = _session(
        summaries=[_summary(1, when=100), _summary(2, when=200)],
        details=[_detail(1, when=100, body="<p>a</p>")],  # "2" missing
    )
    state = {"known_ids": ["1", "2"], "last_when": 50}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    ids = {r["id"] for r in rows}
    assert "1" in ids
    assert {"id": "2", "_deleted": True} in rows
    assert "2" not in state["known_ids"]


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    session = _session(summaries=[])
    state = {"known_ids": ["1", "2"], "last_when": 100}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    assert rows == []
    assert state["known_ids"] == ["1", "2"]


def test_new_question_below_cursor_is_still_ingested():
    session = _session(
        summaries=[_summary(1, when=100), _summary(3, when=100)],
        details=[
            _detail(1, when=100, body="<p>known</p>"),
            _detail(3, when=100, body="<p>new</p>"),
        ],
    )
    state = {"known_ids": ["1"], "last_when": 500}
    rows = list(
        sync_questions(
            session, state, tags=["python"], site=SITE, api_key=None, answers_per_question=3
        )
    )

    assert [r["id"] for r in rows] == ["3"]  # question "1" skipped, new question ingested


def test_tags_are_pushed_down_as_a_semicolon_joined_filter():
    session = _session(summaries=[])
    list(
        sync_questions(
            session,
            {},
            tags=["python", "asyncio"],
            site=SITE,
            api_key="KEY",
            answers_per_question=3,
        )
    )
    url, params = session.calls[0]
    assert url.endswith("/questions")
    assert params["tagged"] == "python;asyncio"
    assert params["key"] == "KEY"
    assert params["site"] == SITE


# ---------------------------------------------------------------------------
# stack_overflow_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_stack_overflow_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = stack_overflow_source(tags=["python"], session=_session([]))
    assert resource.name == "stack_overflow_questions"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_stack_overflow_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = stack_overflow_source(tags=["python"], session=_session([]))
    assert STACK_OVERFLOW_SOURCE_NAME == "stack_overflow"
    assert document_source_tag(resource) == "stack_overflow"


def test_stack_overflow_source_requires_tags():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="tags is required"):
        stack_overflow_source(session=_session([]))


def test_stack_overflow_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee\\[stack-overflow\\]"):
        stack_overflow_source(tags=["python"], session=_session([]))


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_stack_overflow_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "stack_overflow.duckdb")),
        dataset_name="so",
    )

    # Sync #1: two live questions land in the destination.
    session1 = _session(
        summaries=[_summary(1, when=100), _summary(2, when=200)],
        details=[_detail(1, when=100, body="<p>a</p>"), _detail(2, when=200, body="<p>b</p>")],
    )
    pipeline.run(stack_overflow_source(tags=["python"], session=session1))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM stack_overflow_questions")[0][0] == 2

    # Sync #2: question "2" deleted upstream, question "1" unchanged. The
    # connector emits a hard-delete marker for "2"; dlt's merge removes it.
    session2 = _session(
        summaries=[_summary(1, when=100)],
        details=[_detail(1, when=100, body="<p>a</p>")],
    )
    pipeline.run(stack_overflow_source(tags=["python"], session=session2))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM stack_overflow_questions")
    assert [r[0] for r in rows] == ["1"]  # question "2" forgotten, question "1" retained
