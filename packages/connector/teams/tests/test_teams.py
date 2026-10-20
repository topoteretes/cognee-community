"""Unit tests for the Microsoft Teams dlt connector.

Three layers, all runnable in CI without a Microsoft 365 tenant:

* DB-free tests for HTML → text, thread rendering, and the generic document
  DataItem tagging (``source="teams"``) that routes threads through cognify.
* Sync tests against ``FakeGraph`` (a transport adapter on a real requests
  session, so dlt's OAuth2 token flow, ``rest_api`` resolution and
  ``@odata.nextLink`` paging all run), covering the incremental cursor and
  every forget-on-delete path.
* dlt-pipeline tests (temp sqlite destination) proving a tombstone physically
  removes the row on ``merge``, that ``replace`` degrades to a full snapshot,
  and that a failed run keeps its cursor.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from fake_graph import FakeGraph

from cognee_community_connector_teams.graph import graph_client_config, graph_rest_client
from cognee_community_connector_teams.teams import (
    TEAMS_SOURCE_NAME,
    _chain_time,
    _thread_to_row,
    html_to_text,
    list_channels,
    sync_threads,
    teams_source,
)

TEAM = "team-1"
GENERAL = "19:general@thread.tacv2"
RANDOM = "19:random@thread.tacv2"


def _thread_id(channel_id, root_id, team_id=TEAM):
    return f"thread:{team_id}:{channel_id}:{root_id}"


@pytest.fixture
def graph():
    fake = FakeGraph()
    fake.add_team(TEAM, "Engineering")
    fake.add_channel(TEAM, GENERAL, "General")
    fake.add_channel(TEAM, RANDOM, "Random")
    return fake


def _sync(graph, state, team_ids=(TEAM,), channel_ids=None):
    """Run one sync against the fake; return the yielded rows keyed by id."""
    config = graph_client_config("tenant", "client", "secret", session=graph.session())
    channels = list_channels(config, list(team_ids) or None, channel_ids)
    rows = list(sync_threads(graph_rest_client(config), channels, state))
    return {row["id"]: row for row in rows}


def _live(rows):
    return {row_id for row_id, row in rows.items() if not row["_deleted"]}


def _tombstones(rows):
    return {row_id for row_id, row in rows.items() if row["_deleted"]}


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_html_to_text_keeps_mentions_breaks_and_entities():
    raw = (
        '<div><p>Hi <at id="0">Jane Smith</at>,</p><p>ship it &amp; tell me</p>'
        "<ul><li>one</li><li>two</li></ul>line<br>break&nbsp;here</div>"
    )
    assert html_to_text(raw) == (
        "Hi @Jane Smith,\nship it & tell me\n- one\n- two\nline\nbreak here"
    )


def test_html_to_text_handles_empty_and_image_only_bodies():
    assert html_to_text(None) == ""
    assert html_to_text('<div><img src="https://x/y.png"></div>') == ""


def _message(message_id, text, created, author="Robin", **extra):
    return {
        "id": message_id,
        "messageType": "message",
        "createdDateTime": created,
        "lastModifiedDateTime": created,
        "deletedDateTime": None,
        "from": {"user": {"displayName": author}},
        "body": {"contentType": "html", "content": text},
        **extra,
    }


def test_thread_to_row_renders_root_then_replies_in_time_order():
    root = _message("1", "<p>Kickoff</p>", "2026-01-01T09:00:00Z", webUrl="https://teams/1")
    replies = [
        _message("3", "second", "2026-01-01T09:02:00Z", author="Jane"),
        _message("2", "first", "2026-01-01T09:01:00Z", author="Alex"),
        _message("4", "", "2026-01-01T09:03:00Z", deletedDateTime="2026-01-01T09:04:00Z"),
        {**_message("5", "joined", "2026-01-01T09:05:00Z"), "messageType": "systemEventMessage"},
    ]

    row = _thread_to_row(root, replies, "General")

    assert row["title"] == "General: Kickoff"
    assert row["url"] == "https://teams/1"
    assert row["_deleted"] is False
    assert row["content"] == (
        "Robin (2026-01-01 09:00 UTC):\nKickoff\n\n"
        "Reply from Alex (2026-01-01 09:01 UTC):\nfirst\n\n"
        "Reply from Jane (2026-01-01 09:02 UTC):\nsecond"
    )


def test_thread_to_row_prefers_subject_and_lists_attachments():
    root = _message(
        "1",
        "see attached",
        "2026-01-01T09:00:00Z",
        subject="Q3 plan",
        attachments=[{"name": "plan.xlsx", "contentType": "reference"}],
    )

    row = _thread_to_row(root, [], "General")

    assert row["title"] == "General: Q3 plan"
    assert row["content"].endswith("see attached\n[attachment: plan.xlsx]")


def test_thread_to_row_without_text_is_none():
    root = _message("1", '<img src="https://x/y.png">', "2026-01-01T09:00:00Z")
    assert _thread_to_row(root, [], "General") is None


def test_chain_time_is_latest_change_across_the_thread():
    root = _message("1", "a", "2026-01-01T09:00:00Z")
    reply = _message("2", "b", "2026-01-02T09:00:00Z")
    reply["lastModifiedDateTime"] = "2026-01-03T10:00:00.1234567Z"  # 7-digit fraction

    assert _chain_time(root, [reply]).isoformat() == "2026-01-03T10:00:00.123456+00:00"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": _thread_id(GENERAL, "1"),
            "url": "https://teams/1",
            "title": "General: Kickoff",
            "content": "Robin (2026-01-01 09:00 UTC):\nKickoff",
        },
        table_name="teams_threads",
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, row.row_data["id"])

    item = _build_document_data_item(row, data_id, TEAMS_SOURCE_NAME)

    # source="teams" (not "dlt") is what routes the thread through normal cognify.
    assert item.system_metadata["source"] == "teams"
    assert item.system_metadata["external_id"] == _thread_id(GENERAL, "1")
    assert item.data.startswith("# General: Kickoff")


def test_teams_source_declares_document_marker_and_tenant_scope():
    from cognee.tasks.ingestion.dlt_utils import (
        PIPELINE_SCOPE_ATTR,
        document_source_tag,
        pipeline_name_for_source,
    )

    source = teams_source("tenant", "client", "secret", team_ids=[TEAM])

    assert document_source_tag(source) == "teams"
    # Cursors get a pipeline of their own per dataset, not the shared default.
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "teams:tenant"
    assert pipeline_name_for_source(source, "teams") != pipeline_name_for_source(source, "other")
    assert pipeline_name_for_source(source, "teams") != "ingest_dlt_source"


def test_credentials_fall_back_to_env_and_are_required(monkeypatch):
    for name in ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="MS_GRAPH_CLIENT_SECRET"):
        teams_source("tenant", "client")

    monkeypatch.setenv("MS_GRAPH_TENANT_ID", "contoso")
    monkeypatch.setenv("MS_GRAPH_CLIENT_ID", "client")
    monkeypatch.setenv("MS_GRAPH_CLIENT_SECRET", "secret")
    config = graph_client_config()
    assert config["auth"].access_token_url == (
        "https://login.microsoftonline.com/contoso/oauth2/v2.0/token"
    )


# ---------------------------------------------------------------------------
# Listing + ingest path (FakeGraph)
# ---------------------------------------------------------------------------


def test_listing_skips_private_channels_and_honours_selection(graph):
    graph.add_channel(TEAM, "19:secret@thread.tacv2", "Secret", membership="private")
    config = graph_client_config("tenant", "client", "secret", session=graph.session())

    listed = list_channels(config, [TEAM])
    assert {channel["id"] for channel in listed} == {GENERAL, RANDOM}
    assert all(channel["_teams_id"] == TEAM for channel in listed)

    selected = list_channels(config, [TEAM], {RANDOM})
    assert [channel["id"] for channel in selected] == [RANDOM]


def test_listing_without_team_ids_lists_every_team(graph):
    graph.add_team("team-2")
    graph.add_channel("team-2", "19:ops@thread.tacv2", "Ops")
    config = graph_client_config("tenant", "client", "secret", session=graph.session())

    listed = list_channels(config, None)

    assert {(c["_teams_id"], c["id"]) for c in listed} == {
        (TEAM, GENERAL),
        (TEAM, RANDOM),
        ("team-2", "19:ops@thread.tacv2"),
    }


def test_first_sync_ingests_every_thread_across_pages(graph):
    ids = [graph.post(TEAM, GENERAL, f"message {i}") for i in range(60)]
    graph.system_event(TEAM, GENERAL)
    state = {}

    rows = _sync(graph, state)

    # 60 threads need two 50-root pages; the system event is not a document.
    assert _live(rows) == {_thread_id(GENERAL, root_id) for root_id in ids}
    assert len(graph.message_requests()) == 3  # General x2 pages + Random x1
    assert graph.token_requests == 1
    entry = state["channels"][f"{TEAM}:{GENERAL}"]
    assert sorted(entry["threads"]) == sorted(ids)
    assert entry["cursor"]


def test_unchanged_channel_costs_one_request(graph):
    ids = [graph.post(TEAM, GENERAL, f"message {i}") for i in range(60)]
    state = {}
    first = _sync(graph, state)
    graph.requests.clear()

    rows = _sync(graph, state)

    # One page per channel: the walk stops on the first page.
    assert len(graph.message_requests()) == 2
    # Only the cursor's overlap window is re-read (the newest thread here), and
    # it re-renders identically, so cognee keeps its content-hash data_id.
    assert set(rows) == {_thread_id(GENERAL, ids[-1])}
    assert rows == {row_id: first[row_id] for row_id in rows}


def test_new_reply_re_renders_an_old_thread_and_walk_stops_early(graph):
    old = graph.post(TEAM, GENERAL, "old topic")
    for i in range(60):
        graph.post(TEAM, GENERAL, f"filler {i}")
    state = {}
    _sync(graph, state)
    graph.requests.clear()

    graph.reply(TEAM, GENERAL, old, "late answer")
    rows = _sync(graph, state)

    # The reply bumps the whole chain to the top of Graph's ordering; the
    # previous newest thread is re-read only as the cursor's overlap window.
    assert _thread_id(GENERAL, old) in _live(rows)
    assert len(rows) == 2
    assert "Reply from Jane Smith" in rows[_thread_id(GENERAL, old)]["content"]
    assert "late answer" in rows[_thread_id(GENERAL, old)]["content"]
    assert len(graph.message_requests()) == 2


def test_new_thread_is_picked_up_incrementally(graph):
    graph.post(TEAM, GENERAL, "first")
    state = {}
    _sync(graph, state)

    new = graph.post(TEAM, RANDOM, "brand new")
    rows = _sync(graph, state)

    assert _thread_id(RANDOM, new) in _live(rows)


def test_replies_past_the_inline_page_are_fetched(graph):
    graph.inline_reply_limit = 2
    root = graph.post(TEAM, GENERAL, "big thread")
    for i in range(5):
        graph.reply(TEAM, GENERAL, root, f"reply number {i}")

    rows = _sync(graph, {})

    content = rows[_thread_id(GENERAL, root)]["content"]
    for i in range(5):
        assert f"reply number {i}" in content
    # Rendered oldest first, regardless of Graph's newest-first reply paging.
    assert content.index("reply number 0") < content.index("reply number 4")
    assert any(path.endswith("/replies") for _method, path in graph.requests)


def test_edited_and_deleted_replies_re_render_the_thread(graph):
    root = graph.post(TEAM, GENERAL, "question")
    keep = graph.reply(TEAM, GENERAL, root, "draft answer")
    drop = graph.reply(TEAM, GENERAL, root, "wrong answer")
    state = {}
    _sync(graph, state)

    graph.edit(TEAM, GENERAL, keep, "final answer")
    graph.delete(TEAM, GENERAL, drop)
    rows = _sync(graph, state)

    content = rows[_thread_id(GENERAL, root)]["content"]
    assert "final answer" in content
    assert "draft answer" not in content
    assert "wrong answer" not in content
    assert rows[_thread_id(GENERAL, root)]["_deleted"] is False


def test_deleted_root_tombstones_its_thread(graph):
    keep = graph.post(TEAM, GENERAL, "keep me")
    gone = graph.post(TEAM, GENERAL, "delete me")
    state = {}
    _sync(graph, state)

    graph.delete(TEAM, GENERAL, gone)
    rows = _sync(graph, state)

    assert _tombstones(rows) == {_thread_id(GENERAL, gone)}
    assert state["channels"][f"{TEAM}:{GENERAL}"]["threads"] == [keep]


def test_deleted_root_never_synced_emits_no_tombstone(graph):
    gone = graph.post(TEAM, GENERAL, "short lived")
    graph.delete(TEAM, GENERAL, gone)

    assert _sync(graph, {}) == {}


def test_removed_channel_tombstones_every_thread_it_contributed(graph):
    a = graph.post(TEAM, RANDOM, "a")
    b = graph.post(TEAM, RANDOM, "b")
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state)

    graph.remove_channel(TEAM, RANDOM)
    rows = _sync(graph, state)

    assert _tombstones(rows) == {_thread_id(RANDOM, a), _thread_id(RANDOM, b)}
    assert f"{TEAM}:{RANDOM}" not in state["channels"]


def test_deselected_channel_is_forgotten(graph):
    a = graph.post(TEAM, RANDOM, "a")
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state)

    rows = _sync(graph, state, channel_ids={GENERAL})

    assert _tombstones(rows) == {_thread_id(RANDOM, a)}


def test_deleted_team_drops_out_like_a_deleted_channel(graph):
    graph.add_team("team-2")
    graph.add_channel("team-2", "19:ops@thread.tacv2", "Ops")
    ops = graph.post("team-2", "19:ops@thread.tacv2", "ops chatter")
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state, team_ids=(TEAM, "team-2"))

    del graph.teams["team-2"]  # the channel listing now 404s
    rows = _sync(graph, state, team_ids=(TEAM, "team-2"))

    assert _tombstones(rows) == {_thread_id("19:ops@thread.tacv2", ops, team_id="team-2")}


def test_empty_listing_does_not_forget_known_channels(graph):
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state)

    rows = _sync(graph, state, channel_ids={"19:typo@thread.tacv2"})

    assert _tombstones(rows) == set()
    assert f"{TEAM}:{GENERAL}" in state["channels"]


def test_forbidden_listing_aborts_instead_of_forgetting(graph):
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state)

    graph.fail_once("/channels", 403)
    with pytest.raises(Exception, match="403"):
        _sync(graph, state)
    assert f"{TEAM}:{GENERAL}" in state["channels"]


def test_failed_walk_keeps_the_channel_cursor(graph):
    graph.post(TEAM, GENERAL, "c")
    state = {}
    _sync(graph, state)
    before = dict(state["channels"][f"{TEAM}:{GENERAL}"])

    graph.post(TEAM, GENERAL, "new")
    graph.fail_once(f"{GENERAL}/messages", 403)
    with pytest.raises(Exception, match="403"):
        _sync(graph, state, channel_ids={GENERAL})

    assert state["channels"][f"{TEAM}:{GENERAL}"] == before


def test_throttled_requests_are_retried_with_retry_after(graph):
    from dlt.sources.helpers.requests import Client

    root = graph.post(TEAM, GENERAL, "c")
    graph.fail_once(f"{GENERAL}/messages", 429, {"Retry-After": "0"})
    # dlt's retrying client, as used in production, routed to the fake.
    session = Client(raise_for_status=False, request_max_attempts=3).session
    graph.mount_on(session)
    config = graph_client_config("tenant", "client", "secret", session=session)

    channels = list_channels(config, [TEAM], {GENERAL})
    rows = list(sync_threads(graph_rest_client(config), channels, {}))

    assert [row["id"] for row in rows] == [_thread_id(GENERAL, root)]
    assert len(graph.message_requests()) == 2  # the 429, then the retry


# ---------------------------------------------------------------------------
# dlt pipeline: merge + hard_delete, replace fallback, failure safety
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    db_path = (tmp_path / "teams.db").as_posix()
    return dlt.pipeline(
        pipeline_name="teams_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="teams_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(pipeline, graph, **run_kwargs):
    source = teams_source("tenant", "client", "secret", team_ids=[TEAM], session=graph.session())
    return pipeline.run(source, **run_kwargs)


def _read_threads(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM teams_threads") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"title": row[1], "content": row[2]} for row in rows}


def test_pipeline_merges_edits_and_hard_deletes_tombstones(graph, tmp_path):
    keep = graph.post(TEAM, GENERAL, "keep v1")
    gone = graph.post(TEAM, GENERAL, "delete me")
    other = graph.post(TEAM, RANDOM, "other channel")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, graph)
    assert set(_read_threads(pipeline)) == {
        _thread_id(GENERAL, keep),
        _thread_id(GENERAL, gone),
        _thread_id(RANDOM, other),
    }

    graph.edit(TEAM, GENERAL, keep, "keep v2")
    graph.delete(TEAM, GENERAL, gone)
    _run(pipeline, graph)

    rows = _read_threads(pipeline)
    # The tombstone physically removed the row; the untouched thread from the
    # other channel survived the incremental (partial) load.
    assert set(rows) == {_thread_id(GENERAL, keep), _thread_id(RANDOM, other)}
    assert "keep v2" in rows[_thread_id(GENERAL, keep)]["content"]


def test_replace_runs_fall_back_to_full_snapshots(graph, tmp_path):
    keep = graph.post(TEAM, GENERAL, "keep")
    gone = graph.post(TEAM, GENERAL, "delete me")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, graph, write_disposition="replace")

    graph.delete(TEAM, GENERAL, gone)
    graph.requests.clear()
    _run(pipeline, graph, write_disposition="replace")

    # dlt resets resource state under replace, so the run re-walks everything
    # and loads a complete snapshot: unchanged threads stay, deleted ones drop.
    assert set(_read_threads(pipeline)) == {_thread_id(GENERAL, keep)}
    assert len(graph.message_requests()) == 2


def test_failed_run_keeps_cursor_so_the_next_run_catches_up(graph, tmp_path):
    graph.post(TEAM, GENERAL, "c")
    pipeline = _pipeline(tmp_path)
    _run(pipeline, graph)

    late = graph.post(TEAM, RANDOM, "posted during an outage")
    graph.fail_once(f"{RANDOM}/messages", 503)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the error in PipelineStepFailed
        _run(pipeline, graph)
    assert _thread_id(RANDOM, late) not in _read_threads(pipeline)

    _run(pipeline, graph)
    assert _thread_id(RANDOM, late) in _read_threads(pipeline)
