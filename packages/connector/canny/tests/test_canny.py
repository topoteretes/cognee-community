"""Unit tests for the Canny dlt connector (no API key or network needed).

* DB-free tests for row building, paging, retry and document tagging.
* dlt-pipeline tests (fake HTTP client, temp sqlite destination) for the
  acceptance criteria: re-sync reflects edits/new votes, unchanged posts keep
  identical rows (stable ids/content -> not re-cognified), and deleted posts
  drop out of the snapshot (forget-on-delete).
"""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_canny import canny as cn
from cognee_community_connector_canny.canny import (
    CANNY_SOURCE_NAME,
    _iter_comments,
    _iter_posts,
    _post_to_row,
    _request,
    canny_source,
)


def _post(pid, title="Dark mode", score=5, comments=0, status="open", board="Features", **kw):
    post = {
        "id": pid,
        "title": title,
        "details": f"Please add {title}.",
        "score": score,
        "commentCount": comments,
        "status": status,
        "board": {"id": "b1", "name": board},
        "category": {"name": "UI"},
        "author": {"name": "Sally Doe"},
        "tags": [{"name": "ios"}],
        "created": "2026-01-01T00:00:00.000Z",
        "url": f"https://acme.canny.io/p/{pid}",
    }
    post.update(kw)
    return post


def _comment(cid, text, who="Bob", internal=False, created="2026-01-02T00:00:00.000Z"):
    return {
        "id": cid,
        "value": text,
        "author": {"name": who},
        "internal": internal,
        "created": created,
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("POST", cn.CANNY_POSTS_URL), response=self
            )


class FakeCanny:
    """Stand-in for httpx.Client mimicking posts/list (skip) and comments/list."""

    def __init__(self, posts, comments=None):
        self.posts = posts
        self.comments = comments or {}
        self.calls = []

    def post(self, url, json=None):
        body = dict(json)
        self.calls.append((url, body))
        if url == cn.CANNY_POSTS_URL:
            posts = [
                p
                for p in self.posts
                if not body.get("boardID") or p["board"]["id"] == body["boardID"]
            ]
            skip, limit = body["skip"], body["limit"]
            page = posts[skip : skip + limit]
            return FakeResponse(payload={"posts": page, "hasMore": skip + limit < len(posts)})
        assert url == cn.CANNY_COMMENTS_URL
        return FakeResponse(
            payload={"items": self.comments.get(body["postID"], []), "hasNextPage": False}
        )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(cn.time, "sleep", lambda _s: None)


# --- row building ---------------------------------------------------------


def test_row_carries_votes_status_board_and_details():
    row = _post_to_row(_post("p1", score=42, comments=2), [])
    assert row["id"] == "p1"
    assert row["title"] == "Dark mode"
    assert row["url"] == "https://acme.canny.io/p/p1"
    for expected in ("Votes: 42", "Status: open", "Board: Features", "Tags: ios", "Please add"):
        assert expected in row["content"]
    assert set(row) == {"id", "title", "content", "url"}


def test_comments_are_rendered_oldest_first():
    comments = [
        _comment("c2", "second", created="2026-01-03T00:00:00Z"),
        _comment("c1", "first", created="2026-01-02T00:00:00Z"),
    ]
    content = _post_to_row(_post("p1"), comments)["content"]
    assert content.index("first") < content.index("second")


def test_row_is_stable_for_same_input():
    a = _post_to_row(_post("p1"), [_comment("c1", "hi")])
    b = _post_to_row(_post("p1"), [_comment("c1", "hi")])
    assert a == b


# --- paging / retry -------------------------------------------------------


def test_iter_posts_follows_skip_pagination(monkeypatch):
    monkeypatch.setattr(cn, "_PAGE_SIZE", 2)
    client = FakeCanny([_post(f"p{i}") for i in range(5)])
    assert [p["id"] for p in _iter_posts(client, "k", None, None)] == [f"p{i}" for i in range(5)]
    assert [c[1]["skip"] for c in client.calls] == [0, 2, 4]


def test_iter_comments_follows_cursor():
    pages = {
        None: {"items": [{"id": "a"}], "hasNextPage": True, "cursor": "c2"},
        "c2": {"items": [{"id": "b"}], "hasNextPage": False, "cursor": None},
    }

    class Client:
        def post(self, url, json=None):
            return FakeResponse(payload=pages[json.get("cursor")])

    assert [c["id"] for c in _iter_comments(Client(), "k", "p1")] == ["a", "b"]


class _Flaky:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, url, json=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_request_sends_api_key_in_body():
    client = FakeCanny([])
    _request(client, cn.CANNY_POSTS_URL, "secret", {"skip": 0, "limit": 1})
    # FakeCanny records the body; the key travels in the JSON payload.
    assert client.calls[0][1]["limit"] == 1


def test_request_retries_rate_limit():
    client = _Flaky([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {"ok": 1})])
    assert _request(client, "u", "k", {}) == {"ok": 1}
    assert client.calls == 2


def test_request_retries_network_error():
    client = _Flaky([httpx.ConnectError("boom"), FakeResponse(200, {"ok": 1})])
    assert _request(client, "u", "k", {}) == {"ok": 1}


def test_request_gives_up_after_retry_budget():
    client = _Flaky([FakeResponse(503)] * cn._MAX_RETRIES)
    with pytest.raises(httpx.HTTPStatusError):
        _request(client, "u", "k", {})


def test_request_bad_key_raises_permission_error():
    with pytest.raises(PermissionError):
        _request(_Flaky([FakeResponse(401)]), "u", "k", {})


# --- source config --------------------------------------------------------


def test_source_is_document_tagged_and_replace():
    source = canny_source(client=FakeCanny([]))
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == CANNY_SOURCE_NAME
    assert source.name == cn.CANNY_TABLE_NAME
    assert source.write_disposition == "replace"


def test_source_requires_key_when_no_client(monkeypatch):
    monkeypatch.delenv("CANNY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CANNY_API_KEY"):
        canny_source()


# --- dlt pipeline: snapshot + forget-on-delete ----------------------------


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="canny_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'c.db'}"),
        dataset_name="canny",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, source):
    pipeline = _pipeline(tmp_path)
    pipeline.run(source)
    return pipeline


def _rows(pipeline):
    query = f"SELECT id, content FROM {cn.CANNY_TABLE_NAME} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def test_ingests_posts_with_comments_and_skips_internal(tmp_path):
    client = FakeCanny(
        [_post("p1", comments=2)],
        {"p1": [_comment("c1", "public"), _comment("c2", "secret", internal=True)]},
    )
    rows = _rows(_run(tmp_path, canny_source(client=client)))
    assert "public" in rows["p1"]
    assert "secret" not in rows["p1"]


def test_include_internal_keeps_internal_comments(tmp_path):
    client = FakeCanny([_post("p1", comments=1)], {"p1": [_comment("c2", "secret", internal=True)]})
    rows = _rows(_run(tmp_path, canny_source(client=client, include_internal=True)))
    assert "secret" in rows["p1"]


def test_posts_without_comments_do_not_fetch_comments(tmp_path):
    client = FakeCanny([_post("p1", comments=0)])
    _run(tmp_path, canny_source(client=client))
    assert all(url == cn.CANNY_POSTS_URL for url, _ in client.calls)


def test_resync_reflects_new_votes_and_keeps_unchanged_rows_identical(tmp_path):
    first = FakeCanny([_post("p1", score=5), _post("p2", title="SSO", score=9)])
    before = _rows(_run(tmp_path, canny_source(client=first)))

    second = FakeCanny([_post("p1", score=6), _post("p2", title="SSO", score=9)])
    after = _rows(_run(tmp_path, canny_source(client=second)))

    assert "Votes: 6" in after["p1"]
    assert after["p2"] == before["p2"]  # unchanged -> same content -> same data_id


def test_deleted_post_is_removed_on_next_sync(tmp_path):
    first = FakeCanny([_post("p1"), _post("p2", title="SSO")])
    _run(tmp_path, canny_source(client=first))
    second = FakeCanny([_post("p2", title="SSO")])
    assert list(_rows(_run(tmp_path, canny_source(client=second)))) == ["p2"]


def test_failed_run_leaves_previous_snapshot_untouched(tmp_path):
    _run(tmp_path, canny_source(client=FakeCanny([_post("p1")])))
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, canny_source(client=_Flaky([FakeResponse(400)])))
    assert list(_rows(_pipeline(tmp_path))) == ["p1"]


def test_board_filter_is_sent_and_applied(tmp_path):
    bugs = _post("p2", board="Bugs")
    bugs["board"]["id"] = "b2"
    client = FakeCanny([_post("p1"), bugs])
    rows = _rows(_run(tmp_path, canny_source(client=client, board_ids=["b2"])))
    assert list(rows) == ["p2"]
