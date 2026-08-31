"""Unit tests for the Reddit dlt connector.

Two layers, all runnable in CI with no live Reddit and no sleeping:

* DB-free tests for config normalization (including the ``REDDIT_*`` env
  fallbacks), subreddit-name normalization, submission rendering (self post,
  link post, indented comment tree, truncation notice), budgeted comment-tree
  walking (a ``more`` node expanded under budget and refused over budget), and
  the pure ``sync_submissions`` state machine (first ingest, the
  ``before``/``after`` cursor, the unchanged-vs-changed re-emission gate,
  ``_deleted`` tombstones for vanished/removed/self-deleted submissions, and
  idempotence on a replayed batch), with a plain dict standing in for dlt's
  resource state and hand-built API payloads.
* dlt-pipeline tests (temp sqlite destination, fake OAuth client) covering the
  acceptance criteria end to end: first sync loads submissions and their
  comments, an incremental second sync picks up only what changed and upserts
  on ``merge``, and a submission deleted upstream drops out via the
  ``_deleted`` hard-delete marker.
"""

import json

import pytest

from cognee_community_connector_reddit.reddit import (
    REDDIT_SOURCE_NAME,
    collect_comments,
    is_gone,
    normalize_config,
    normalize_subreddit,
    render_comment_lines,
    render_submission,
    sync_submissions,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SUB = "localllama"
_OTHER = "cognee"


def _submission(
    ident: str,
    title: str = "Ship the connector",
    *,
    subreddit: str = _SUB,
    selftext: str = "We should ship it today.",
    author: str = "alice",
    created: float = 1756500000,
    score: int = 42,
    num_comments: int = 3,
    **extra,
) -> dict:
    return {
        "id": ident,
        "name": f"t3_{ident}",
        "subreddit": subreddit,
        "title": title,
        "selftext": selftext,
        "author": author,
        "created_utc": created,
        "score": score,
        "num_comments": num_comments,
        "is_self": True,
        "permalink": f"/r/{subreddit}/comments/{ident}/ship_the_connector/",
        **extra,
    }


def _t1(ident: str, body: str, *, author: str = "bob", score: int = 5, replies=None) -> dict:
    data = {"id": ident, "author": author, "body": body, "score": score, "created_utc": 1756500100}
    if replies:
        data["replies"] = {"kind": "Listing", "data": {"children": list(replies)}}
    return {"kind": "t1", "data": data}


def _more(children, *, parent: str | None = None) -> dict:
    return {"kind": "more", "data": {"children": list(children), "parent_id": parent, "count": 9}}


def _grafted(ident: str, body: str, *, parent: str) -> dict:
    """A ``t1`` as /api/morechildren returns it: flat, with a parent_id."""
    thing = _t1(ident, body)
    thing["data"]["parent_id"] = parent
    return thing


def _bundle(submission: dict, comments=(), *, truncated: bool = False) -> dict:
    return {"submission": submission, "comments": list(comments), "truncated": truncated}


def _tree(children, *, more_calls=None, max_depth: int = 10, max_more_requests: int = 10):
    """Run collect_comments with a scripted fake /api/morechildren."""
    calls: list[list[str]] = []
    scripted = list(more_calls or [])

    def fetch_more(child_ids):
        calls.append(list(child_ids))
        return scripted.pop(0) if scripted else []

    nodes, truncated, used = collect_comments(
        fetch_more, children, max_depth=max_depth, max_more_requests=max_more_requests
    )
    return nodes, truncated, used, calls


def _run(state: dict, listings, info=None) -> list[dict]:
    return list(sync_submissions(normalize_config(), state, listings, info))


def _live_rows(rows) -> dict[str, dict]:
    return {row["id"]: row for row in rows if not row.get("_deleted")}


def _deleted_ids(rows) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


# ---------------------------------------------------------------------------
# Config + subreddit normalization
# ---------------------------------------------------------------------------


def test_subreddit_name_forms_all_normalize():
    for raw in ("python", "Python", "r/Python", "/r/python/", "https://www.reddit.com/r/Python/"):
        assert normalize_subreddit(raw) == "python"


def test_subreddit_normalization_rejects_empty():
    assert normalize_subreddit(None) is None
    assert normalize_subreddit("   ") is None
    assert normalize_subreddit("/r/") is None


def test_normalize_config_dedupes_preserving_order():
    config = normalize_config(["r/Python", "cognee", "/r/python/", ""])
    assert config.subreddits == ("python", "cognee")


def test_normalize_config_clamps_budgets():
    config = normalize_config([], comment_depth=0, max_more_requests=-5, backfill_limit=0)
    assert config.comment_depth == 1
    assert config.max_more_requests == 0  # 0 is a legitimate "no expansion at all"
    assert config.backfill_limit == 1


# ---------------------------------------------------------------------------
# Submission rendering
# ---------------------------------------------------------------------------


def test_self_post_becomes_document_row():
    row, fingerprint = render_submission(_submission("abc123"))
    assert row["id"] == "t3_abc123"
    assert row["title"] == "Ship the connector"
    assert row["content"].startswith("# Ship the connector")
    assert "We should ship it today." in row["content"]
    assert "- subreddit: r/localllama" in row["content"]
    assert "- author: u/alice" in row["content"]
    assert "- posted: 2025-08-29T20:40:00Z" in row["content"]
    assert row["url"].startswith("https://www.reddit.com/r/localllama/comments/abc123/")
    assert row["_deleted"] is False
    metadata = json.loads(row["metadata"])
    assert metadata["subreddit"] == "localllama"
    assert metadata["author"] == "alice"
    assert metadata["score"] == 42
    assert metadata["num_comments"] == 3
    assert metadata["comments_truncated"] is False
    assert len(fingerprint) == 64


def test_link_post_renders_the_outbound_link():
    row, _ = render_submission(
        _submission("lnk", selftext="", is_self=False, url="https://example.com/paper.pdf")
    )
    assert "Link post pointing at https://example.com/paper.pdf" in row["content"]
    assert "- links out to: https://example.com/paper.pdf" in row["content"]
    assert json.loads(row["metadata"])["is_self"] is False


def test_flair_lands_in_content_and_metadata():
    row, _ = render_submission(_submission("f1", link_flair_text="Discussion"))
    assert "- flair: Discussion" in row["content"]
    assert json.loads(row["metadata"])["flair"] == "Discussion"


def test_comment_tree_is_rendered_with_indentation():
    nodes, *_ = _tree([_t1("c1", "top level", replies=[_t1("c2", "a reply")])])
    row, _ = render_submission(_submission("abc123"), nodes)
    assert "- **u/bob** (5 points): top level" in row["content"]
    assert "  - **u/bob** (5 points): a reply" in row["content"]


def test_multiline_comment_bodies_stay_indented():
    lines = render_comment_lines([{"author": "bob", "body": "line one\nline two", "replies": []}])
    assert lines == ["- **u/bob**: line one", "  line two"]


def test_submission_without_comments_says_so():
    row, _ = render_submission(_submission("abc123"), [])
    assert "Comments: (none ingested)" in row["content"]


def test_truncation_is_stated_in_the_document():
    row, _ = render_submission(_submission("abc123"), [], truncated=True)
    assert "Comment tree truncated" in row["content"]
    assert json.loads(row["metadata"])["comments_truncated"] is True


def test_render_without_an_id_is_skipped():
    assert render_submission({"title": "orphan"}) is None


def test_fingerprint_ignores_vote_churn_but_tracks_text():
    _, base = render_submission(_submission("abc123"))
    _, voted = render_submission(_submission("abc123", score=999, num_comments=77))
    _, edited = render_submission(_submission("abc123", selftext="Actually, ship it tomorrow."))
    assert base == voted  # score churn must not re-cognify the corpus
    assert base != edited


def test_fingerprint_tracks_the_comment_tree():
    _, bare = render_submission(_submission("abc123"), [])
    nodes, *_ = _tree([_t1("c1", "hello")])
    _, with_comment = render_submission(_submission("abc123"), nodes)
    assert bare != with_comment


# ---------------------------------------------------------------------------
# Comment-tree walking under the expansion budget (the issue's trap)
# ---------------------------------------------------------------------------


def test_nested_replies_are_parsed():
    nodes, truncated, used, calls = _tree(
        [_t1("c1", "top", replies=[_t1("c2", "mid", replies=[_t1("c3", "deep")])])]
    )
    assert truncated is False
    assert used == 0 and calls == []
    assert nodes[0]["replies"][0]["replies"][0]["body"] == "deep"


def test_more_node_is_expanded_within_budget_and_grafted_onto_its_parent():
    nodes, truncated, used, calls = _tree(
        [_t1("c1", "top", replies=[_more(["x1", "x2"], parent="t1_c1")])],
        more_calls=[[_grafted("x1", "hidden one", parent="t1_c1")]],
    )
    assert calls == [["x1", "x2"]]
    assert used == 1
    assert truncated is False
    assert [node["body"] for node in nodes[0]["replies"]] == ["hidden one"]


def test_top_level_more_grafts_to_the_root_when_the_parent_is_the_submission():
    nodes, truncated, _, _ = _tree(
        [_t1("c1", "top"), _more(["x1"], parent="t3_abc123")],
        more_calls=[[_grafted("x1", "from more", parent="t3_abc123")]],
    )
    assert [node["body"] for node in nodes] == ["top", "from more"]
    assert truncated is False


def test_more_node_over_budget_is_truncated_not_expanded():
    nodes, truncated, used, calls = _tree(
        [_t1("c1", "top", replies=[_more(["x1"], parent="t1_c1")])],
        max_more_requests=0,
    )
    assert calls == []  # zero budget means zero calls — one thread can't cost thousands
    assert used == 0
    assert truncated is True
    assert nodes[0]["replies"] == []


def test_more_expansion_stops_at_the_call_budget():
    # Each expansion answers with another `more`, so an unbudgeted walk would
    # never end; the budget must stop it and report truncation.
    chained = [[_more(["x2"], parent="t1_c1")], [_more(["x3"], parent="t1_c1")]]
    _, truncated, used, calls = _tree(
        [_more(["x1"], parent="t3_abc123")], more_calls=chained, max_more_requests=2
    )
    assert used == 2 and len(calls) == 2
    assert truncated is True


def test_bare_continue_this_thread_placeholder_counts_as_truncation():
    _, truncated, used, calls = _tree([_more([], parent="t1_c1")])
    assert calls == [] and used == 0
    assert truncated is True


def test_depth_budget_drops_replies_that_are_too_deep():
    nodes, truncated, _, _ = _tree([_t1("c1", "top", replies=[_t1("c2", "too deep")])], max_depth=1)
    assert nodes[0]["replies"] == []
    assert truncated is True


def test_grafted_node_deeper_than_the_budget_is_dropped():
    _, truncated, _, _ = _tree(
        [_t1("c1", "top", replies=[_more(["x1"], parent="t1_c1")])],
        more_calls=[[_grafted("x1", "way down", parent="t1_c1")]],
        max_depth=1,
    )
    # The `more` sits at depth 2 already, so nothing survives the graft.
    assert truncated is True


# ---------------------------------------------------------------------------
# is_gone readings
# ---------------------------------------------------------------------------


def test_missing_from_info_is_gone():
    assert is_gone(None) is True


def test_moderator_removal_is_gone():
    assert is_gone(_submission("abc123", removed_by_category="moderator")) is True


def test_author_and_body_tombstoned_is_gone():
    assert is_gone(_submission("abc123", author="[deleted]", selftext="[removed]")) is True


def test_a_live_submission_is_not_gone():
    assert is_gone(_submission("abc123")) is False


def test_deleted_author_alone_is_not_gone():
    # Users delete their account without deleting the post; the post survives.
    assert is_gone(_submission("abc123", author="[deleted]")) is False


# ---------------------------------------------------------------------------
# Pure sync state machine
# ---------------------------------------------------------------------------


def test_first_sync_ingests_and_sets_the_listing_cursor():
    state: dict = {}
    rows = _live_rows(
        _run(
            state,
            {
                _SUB: [
                    _bundle(_submission("old", created=1756500000)),
                    _bundle(_submission("new", created=1756600000)),
                ],
                _OTHER: [_bundle(_submission("z1", subreddit=_OTHER))],
            },
        )
    )
    assert set(rows) == {"t3_old", "t3_new", "t3_z1"}
    assert state["subreddits"][_SUB]["newest"] == "t3_new"
    assert state["subreddits"][_SUB]["newest_created"] == 1756600000
    assert set(state["subreddits"][_SUB]["submissions"]) == {"t3_old", "t3_new"}


def test_unchanged_submission_is_not_re_emitted():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    assert _run(state, {_SUB: [_bundle(_submission("abc123"))]}) == []


def test_replayed_batch_is_idempotent():
    state: dict = {}
    listings = {_SUB: [_bundle(_submission("a1")), _bundle(_submission("a2", created=1756600000))]}
    first = _live_rows(_run(state, listings))
    assert _run(state, listings) == []
    assert set(first) == {"t3_a1", "t3_a2"}
    assert state["subreddits"][_SUB]["newest"] == "t3_a2"


def test_edited_submission_is_re_emitted():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    rows = _live_rows(_run(state, {_SUB: [_bundle(_submission("abc123", selftext="rewritten"))]}))
    assert set(rows) == {"t3_abc123"}
    assert "rewritten" in rows["t3_abc123"]["content"]


def test_a_new_reply_re_emits_the_submission():
    state: dict = {}
    nodes, *_ = _tree([_t1("c1", "first")])
    _run(state, {_SUB: [_bundle(_submission("abc123"), nodes)]})
    grown, *_ = _tree([_t1("c1", "first"), _t1("c2", "second")])
    rows = _live_rows(_run(state, {_SUB: [_bundle(_submission("abc123"), grown)]}))
    assert "second" in rows["t3_abc123"]["content"]


def test_a_comment_deleted_inside_a_live_submission_re_emits_without_it():
    # The honest boundary: no instant signal, but the next re-render notices.
    state: dict = {}
    full, *_ = _tree([_t1("c1", "keep me"), _t1("c2", "delete me")])
    _run(state, {_SUB: [_bundle(_submission("abc123"), full)]})
    pruned, *_ = _tree([_t1("c1", "keep me")])
    rows = _live_rows(_run(state, {_SUB: [_bundle(_submission("abc123"), pruned)]}))
    assert "delete me" not in rows["t3_abc123"]["content"]
    assert "keep me" in rows["t3_abc123"]["content"]


def test_score_churn_alone_does_not_re_emit():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123", score=1))]})
    assert _run(state, {_SUB: [_bundle(_submission("abc123", score=900))]}) == []


def test_cursor_only_moves_forward_when_older_submissions_are_refreshed():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("new", created=1756600000))]})
    # A refresh pass re-renders an older submission; the anchor must not slip back.
    _run(state, {_SUB: [_bundle(_submission("old", created=1756000000))]})
    assert state["subreddits"][_SUB]["newest"] == "t3_new"


def test_empty_round_changes_nothing():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    assert _run(state, {_SUB: []}) == []
    assert state["subreddits"][_SUB]["newest"] == "t3_abc123"


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------


def test_submission_missing_from_info_is_tombstoned():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("gone")), _bundle(_submission("stays"))]})
    rows = _run(state, {}, {"t3_gone": None, "t3_stays": _submission("stays")})
    assert _deleted_ids(rows) == {"t3_gone"}
    assert _live_rows(rows) == {}
    assert set(state["subreddits"][_SUB]["submissions"]) == {"t3_stays"}


def test_moderator_removed_submission_is_tombstoned():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    info = {"t3_abc123": _submission("abc123", removed_by_category="moderator")}
    assert _deleted_ids(_run(state, {}, info)) == {"t3_abc123"}


def test_self_deleted_submission_is_tombstoned():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    info = {"t3_abc123": _submission("abc123", author="[deleted]", selftext="[removed]")}
    assert _deleted_ids(_run(state, {}, info)) == {"t3_abc123"}


def test_tombstone_is_emitted_once_and_the_id_is_forgotten():
    state: dict = {}
    _run(state, {_SUB: [_bundle(_submission("abc123"))]})
    assert _deleted_ids(_run(state, {}, {"t3_abc123": None})) == {"t3_abc123"}
    assert _run(state, {}, {"t3_abc123": None}) == []


def test_unknown_id_reported_gone_is_not_tombstoned():
    state: dict = {}
    assert _run(state, {}, {"t3_never_seen": None}) == []


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------

_ENV_VARS = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDIT_PASSWORD",
    "REDDIT_USER_AGENT",
    "REDDIT_REFRESH_TOKEN",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_source_requires_client_credentials(clean_env):
    pytest.importorskip("dlt")
    from cognee_community_connector_reddit import reddit_source

    with pytest.raises(ValueError, match="client_id and client_secret are required"):
        reddit_source(["python"])


def test_source_requires_a_grant(clean_env):
    pytest.importorskip("dlt")
    from cognee_community_connector_reddit import reddit_source

    clean_env.setenv("REDDIT_CLIENT_ID", "id")
    clean_env.setenv("REDDIT_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="username and password are required"):
        reddit_source(["python"])


def test_source_reads_credentials_from_the_environment(clean_env):
    pytest.importorskip("dlt")
    from cognee_community_connector_reddit import reddit_source

    for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"):
        clean_env.setenv(name, "x")
    # Builds without touching the network: the OAuth grant is lazy.
    assert reddit_source(["r/Python"]) is not None


def test_refresh_token_replaces_the_password_grant(clean_env):
    pytest.importorskip("dlt")
    from cognee_community_connector_reddit import reddit_source

    clean_env.setenv("REDDIT_CLIENT_ID", "id")
    clean_env.setenv("REDDIT_CLIENT_SECRET", "secret")
    assert reddit_source(["python"], refresh_token="rt") is not None


def test_source_declares_the_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_reddit import reddit_source

    source = reddit_source([_SUB], client=_FakeClient({}))
    assert REDDIT_SOURCE_NAME == "reddit"
    assert document_source_tag(source) == "reddit"


# ---------------------------------------------------------------------------
# dlt pipeline: ingest + incremental cursor + forget-on-delete end to end
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stands in for the Reddit OAuth client — no network, no sleeping.

    Holds newest-first submission lists per subreddit and a comment tree per
    submission id, and implements exactly the surface ``reddit_source`` uses:
    ``subscribed_subreddits`` / ``listing`` / ``comments`` / ``more_children``
    / ``info``.
    """

    def __init__(self, submissions: dict, comments: dict | None = None, subscribed=(_SUB,)):
        self.submissions = {sub: list(items) for sub, items in submissions.items()}
        self.comments_by_id = dict(comments or {})
        self.subscribed = list(subscribed)
        self.listing_calls: list[tuple] = []

    # -- test-side mutation helpers ----------------------------------------
    def publish(self, subreddit: str, submission: dict) -> None:
        """A brand new submission appears at the top of /new."""
        self.submissions.setdefault(subreddit, []).insert(0, submission)

    def edit(self, subreddit: str, submission: dict) -> None:
        """An existing submission's payload changes in place."""
        items = self.submissions.setdefault(subreddit, [])
        for index, item in enumerate(items):
            if item["name"] == submission["name"]:
                items[index] = submission
                return
        raise AssertionError(f"{submission['name']} is not published")

    def delete(self, fullname: str) -> None:
        """The submission vanishes from Reddit entirely (and from /api/info)."""
        for items in self.submissions.values():
            items[:] = [item for item in items if item["name"] != fullname]

    # -- client surface ----------------------------------------------------
    def subscribed_subreddits(self):
        return list(self.subscribed)

    def listing(self, subreddit, *, before=None, after=None, limit=100):
        self.listing_calls.append((subreddit, before, after))
        items = list(self.submissions.get(subreddit, ()))  # newest first
        if before:
            names = [item["name"] for item in items]
            items = items[: names.index(before)] if before in names else []
        return {
            "children": [{"kind": "t3", "data": item} for item in items[:limit]],
            "after": None,
        }

    def comments(self, submission_id, *, depth, limit):
        children = self.comments_by_id.get(submission_id, [])
        return [
            {"kind": "Listing", "data": {"children": []}},
            {"kind": "Listing", "data": {"children": children}},
        ]

    def more_children(self, link_fullname, children, *, depth):
        return []

    def info(self, fullnames):
        wanted = set(fullnames)
        return [
            item for items in self.submissions.values() for item in items if item["name"] in wanted
        ]


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_pipeline(dlt, tmp_path, client, **kwargs):
    """Run reddit_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_reddit import reddit_source

    db_path = (tmp_path / "reddit.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="reddit_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="reddit_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(reddit_source(client=client, **kwargs))
    return pipeline


def _read_submissions(pipeline):
    """Return {id: row-dict} for reddit_submissions (positional read — dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without ``description``)."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content, url FROM reddit_submissions") as cursor,
    ):
        rows = cursor.fetchall()
    return {
        row[0]: {"id": row[0], "title": row[1], "content": row[2], "url": row[3]} for row in rows
    }


def test_pipeline_first_sync_loads_submissions_and_comments(dlt_mod, tmp_path):
    client = _FakeClient(
        {_SUB: [_submission("s2", created=1756600000), _submission("s1")]},
        comments={"s1": [_t1("c1", "great idea", replies=[_t1("c2", "agreed")])]},
    )
    pipeline = _run_pipeline(dlt_mod, tmp_path, client, subreddits=["r/LocalLLaMA"])

    rows = _read_submissions(pipeline)
    assert set(rows) == {"t3_s1", "t3_s2"}
    assert "- **u/bob** (5 points): great idea" in rows["t3_s1"]["content"]
    assert "  - **u/bob** (5 points): agreed" in rows["t3_s1"]["content"]
    assert rows["t3_s1"]["url"].startswith("https://www.reddit.com/r/localllama/comments/s1/")


def test_pipeline_falls_back_to_subscribed_subreddits(dlt_mod, tmp_path):
    client = _FakeClient({_SUB: [_submission("s1")]}, subscribed=["r/LocalLLaMA"])
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)
    assert set(_read_submissions(pipeline)) == {"t3_s1"}


def test_pipeline_incremental_sync_uses_the_before_cursor(dlt_mod, tmp_path):
    client = _FakeClient({_SUB: [_submission("s1")]})
    _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])
    assert client.listing_calls == [(_SUB, None, None)]  # backfill with `after`

    client.publish(_SUB, _submission("s2", title="Second post", created=1756600000))
    pipeline = _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])

    # The second run anchored on the newest fullname from the first run.
    assert client.listing_calls[1] == (_SUB, "t3_s1", None)
    rows = _read_submissions(pipeline)
    assert set(rows) == {"t3_s1", "t3_s2"}
    assert rows["t3_s2"]["title"] == "Second post"


def test_pipeline_edit_upserts_on_merge(dlt_mod, tmp_path):
    client = _FakeClient({_SUB: [_submission("s1", selftext="v1")]})
    _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])

    client.edit(_SUB, _submission("s1", selftext="v2"))
    pipeline = _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])

    rows = _read_submissions(pipeline)
    assert len(rows) == 1  # merge upserted rather than duplicating
    assert "v2" in rows["t3_s1"]["content"]
    assert "v1" not in rows["t3_s1"]["content"]


def test_pipeline_deleted_submission_drops_out_on_resync(dlt_mod, tmp_path):
    client = _FakeClient(
        {_SUB: [_submission("s2", created=1756600000), _submission("s1")]},
    )
    _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])
    assert set(_read_submissions(_run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB]))) == {
        "t3_s1",
        "t3_s2",
    }

    client.delete("t3_s1")
    pipeline = _run_pipeline(dlt_mod, tmp_path, client, subreddits=[_SUB])

    # The _deleted hard-delete marker removed the row on merge; cognee's
    # orphan_cleanup forgets it (and its comment tree) downstream.
    rows = _read_submissions(pipeline)
    assert "t3_s1" not in rows
    assert "t3_s2" in rows
