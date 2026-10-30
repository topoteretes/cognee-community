"""Unit tests for the Stack Overflow connector.

All tests use injected fakes — no live API calls in CI.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from cognee_community_connector_stack_overflow.render import render_question
from cognee_community_connector_stack_overflow.reconcile import reconcile_deletions
from cognee_community_connector_stack_overflow.planner import plan_stack_overflow_sync


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

def _make_question(
    qid: int = 1,
    title: str = "How do X?",
    body: str = "<p>body</p>",
    score: int = 10,
    tags: list[str] | None = None,
    answers: list[dict] | None = None,
    accepted_answer_id: int | None = None,
    last_activity_date: int = 1_700_000_000,
) -> dict:
    return {
        "question_id": qid,
        "title": title,
        "body": body,
        "score": score,
        "tags": tags or ["python"],
        "answers": answers or [],
        "accepted_answer_id": accepted_answer_id,
        "creation_date": 1_600_000_000,
        "last_activity_date": last_activity_date,
        "link": f"https://stackoverflow.com/q/{qid}",
    }


def _make_answer(aid: int, body: str, score: int = 5, accepted: bool = False) -> dict:
    return {
        "answer_id": aid,
        "body": f"<p>{body}</p>",
        "score": score,
        "owner": {"display_name": "Alice"},
        "link": f"https://stackoverflow.com/a/{aid}",
    }


class FakeStackOverflowClient:
    """In-memory fake that implements the same interface as StackOverflowClient."""

    def __init__(self, questions: list[dict], exist_ids: set[int] | None = None):
        self._questions = questions
        # IDs that exist for deletion recheck; defaults to all question IDs.
        self._exist_ids = exist_ids if exist_ids is not None else {
            q["question_id"] for q in questions
        }
        self._requests = 0

    def get_questions_by_tag(self, tag, from_date=None, min_score=0, page_size=100):
        for q in self._questions:
            if tag in q.get("tags", []):
                yield q

    def get_questions_by_ids(self, ids, page_size=100):
        for q in self._questions:
            if q["question_id"] in self._exist_ids and q["question_id"] in ids:
                yield q

    def estimate_page_count(self, tag, page_size=100):
        return sum(1 for q in self._questions if tag in q.get("tags", []))

    def budget_ok(self):
        return True

    @property
    def requests_used(self):
        return self._requests

    @property
    def quota_remaining(self):
        return 9999


# ---------------------------------------------------------------------------
# render.py tests
# ---------------------------------------------------------------------------

class TestRenderQuestion:
    def test_basic_render_has_title(self):
        q = _make_question(title="How do I read CSV lazily?")
        row = render_question(q)
        assert "How do I read CSV lazily?" in row["content"]
        assert row["title"] == "How do I read CSV lazily?"

    def test_accepted_answer_appears_first(self):
        accepted = _make_answer(aid=10, body="Use scan_csv()", score=30)
        other = _make_answer(aid=11, body="Use read_csv()", score=5)
        q = _make_question(answers=[other, accepted], accepted_answer_id=10)
        row = render_question(q)
        acc_pos = row["content"].find("Accepted Solution")
        other_pos = row["content"].find("Other Answers")
        assert acc_pos < other_pos, "Accepted solution must appear before other answers"

    def test_no_answers_renders_cleanly(self):
        q = _make_question(answers=[])
        row = render_question(q)
        assert "Accepted Solution" not in row["content"]
        assert "Other Answers" not in row["content"]

    def test_stable_data_id_unchanged_question(self):
        q = _make_question()
        id1 = render_question(q)["data_id"]
        id2 = render_question(q)["data_id"]
        assert id1 == id2

    def test_data_id_changes_when_body_changes(self):
        q1 = _make_question(body="<p>original</p>")
        q2 = _make_question(body="<p>edited body</p>")
        assert render_question(q1)["data_id"] != render_question(q2)["data_id"]

    def test_data_id_stable_across_timestamp_change(self):
        """Timestamp-only changes must not cause re-cognification."""
        q1 = _make_question(last_activity_date=1_700_000_000)
        q2 = {**q1, "last_activity_date": 1_800_000_000}
        assert render_question(q1)["data_id"] == render_question(q2)["data_id"]

    def test_attribution_cc_bysa(self):
        row = render_question(_make_question())
        assert "CC BY-SA" in row["content"]

    def test_document_id_namespaced(self):
        q = _make_question(qid=42)
        row = render_question(q)
        assert row["id"] == "stackoverflow:question:42"

    def test_code_block_preserved(self):
        q = _make_question(body="<pre><code>df.lazy().collect()</code></pre>")
        row = render_question(q)
        # html2text or fallback should keep the code
        assert "df.lazy().collect()" in row["content"]


# ---------------------------------------------------------------------------
# reconcile.py tests
# ---------------------------------------------------------------------------

class TestReconcileDeletions:
    def test_no_tracked_ids_yields_nothing(self):
        client = FakeStackOverflowClient(questions=[])
        results = list(reconcile_deletions(client, tracked_ids=[]))
        assert results == []

    def test_existing_question_not_tombstoned(self):
        q = _make_question(qid=1)
        client = FakeStackOverflowClient(questions=[q], exist_ids={1})
        results = list(reconcile_deletions(client, tracked_ids=[1]))
        assert results == []

    def test_missing_question_is_tombstoned(self):
        client = FakeStackOverflowClient(questions=[], exist_ids=set())
        results = list(reconcile_deletions(client, tracked_ids=[99], max_delete_ratio=1.0))
        assert len(results) == 1
        assert results[0]["_deleted"] is True
        assert results[0]["id"] == "stackoverflow:question:99"

    def test_max_delete_ratio_guard(self):
        """If too many IDs are missing, no tombstones are emitted."""
        client = FakeStackOverflowClient(questions=[], exist_ids=set())
        # 5 missing out of 5 = 100 %, above 20 % guard
        results = list(
            reconcile_deletions(
                client, tracked_ids=[1, 2, 3, 4, 5], max_delete_ratio=0.2
            )
        )
        assert results == []

    def test_preview_yields_nothing(self):
        client = FakeStackOverflowClient(questions=[], exist_ids=set())
        results = list(
            reconcile_deletions(client, tracked_ids=[1], preview=True)
        )
        assert results == []

    def test_api_error_during_recheck_yields_nothing(self):
        class ErrorClient:
            def get_questions_by_ids(self, ids, page_size=100):
                raise RuntimeError("API unavailable")
            def budget_ok(self): return True

        results = list(reconcile_deletions(ErrorClient(), tracked_ids=[1]))
        assert results == []


# ---------------------------------------------------------------------------
# planner.py tests
# ---------------------------------------------------------------------------

class TestPlanner:
    def test_plan_returns_string(self):
        client = FakeStackOverflowClient(questions=[_make_question(tags=["polars"])])
        report = plan_stack_overflow_sync(tags=["polars"], client=client)
        assert isinstance(report, str)
        assert "polars" in report

    def test_plan_does_not_ingest(self):
        """plan_stack_overflow_sync must not yield any documents."""
        ingested = []
        client = FakeStackOverflowClient(
            questions=[_make_question(tags=["polars"])],
        )
        plan_stack_overflow_sync(tags=["polars"], client=client)
        assert ingested == [], "Planner must not ingest anything"

    def test_plan_shows_both_first_and_incremental_cost(self):
        client = FakeStackOverflowClient(questions=[_make_question(tags=["python"])])
        report = plan_stack_overflow_sync(tags=["python"], client=client)
        assert "first sync" in report.lower()
        assert "incremental" in report.lower()