"""Unit tests for the Matrix connector.

The Client-Server API is mocked by ``FakeMatrixSession`` — no network, no live
homeserver, so these run in CI. Coverage:

  - first sync ingests text messages and stores the ``next_batch`` cursor
  - the next run sends ``since=<cursor>`` and yields only new events
  - a limited timeline is backfilled via ``/messages`` and stops at known history
  - redactions (pre- and post-v11 shape) become hard-delete markers
  - edits update the original event's row; foreign-sender edits are ignored
  - encrypted and non-text events are skipped
  - thread roots / replies are captured
  - leaving a room forgets everything ingested from it (explicit policy)
  - forget_on_leave=False retains rows after leave
  - edit-before-original in the same batch still updates the original id
  - edits cannot resurrect a previously redacted message
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a real dlt merge removes a redacted message (end-to-end forget-on-delete)
"""

import json
import re

import pytest

from cognee_community_connector_matrix.matrix import matrix_source, sync_messages

HS = "https://matrix.test"
ROOM = "!room:matrix.test"


def _msg(event_id, ts, body, sender="@alice:matrix.test", msgtype="m.text", relates=None):
    content = {"msgtype": msgtype, "body": body}
    if relates:
        content["m.relates_to"] = relates
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": content,
    }


def _edit(event_id, ts, original_id, body, sender="@alice:matrix.test"):
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {"msgtype": "m.text", "body": body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
        },
    }


def _redaction(event_id, ts, target, v11=False):
    event = {
        "type": "m.room.redaction",
        "event_id": event_id,
        "sender": "@alice:matrix.test",
        "origin_server_ts": ts,
        "content": {},
    }
    if v11:
        event["content"]["redacts"] = target
    else:
        event["redacts"] = target
    return event


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeMatrixSession:
    """Serves queued ``/sync`` responses and canned ``/messages`` pages."""

    def __init__(self, syncs, messages=None):
        self.syncs = list(syncs)
        self.messages = messages or {}  # from_token -> {"chunk": [...], "end": ...}
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        self.calls.append((url, params))
        if url.endswith("/_matrix/client/v3/sync"):
            return _Resp(self.syncs.pop(0))
        if re.search(r"/rooms/[^/]+/messages$", url):
            assert params["dir"] == "b"
            return _Resp(self.messages[params["from"]])
        raise AssertionError(f"unexpected URL: {url}")


def _sync(next_batch, events=None, *, limited=False, prev_batch=None, name=None, leave=None):
    join = {}
    if events is not None:
        state = [{"type": "m.room.name", "content": {"name": name}}] if name else []
        join[ROOM] = {
            "state": {"events": state},
            "timeline": {"events": events, "limited": limited, "prev_batch": prev_batch},
        }
    return {"next_batch": next_batch, "rooms": {"join": join, "leave": leave or {}}}


def _run(session, state, **kwargs):
    return list(sync_messages(session, HS, state, **kwargs))


# ---------------------------------------------------------------------------
# Initial + incremental sync
# ---------------------------------------------------------------------------
def test_first_sync_ingests_messages_and_stores_cursor():
    session = FakeMatrixSession(
        [_sync("s1", [_msg("$1", 1000, "hello"), _msg("$2", 2000, "world")], name="Dev")]
    )
    state = {}
    rows = _run(session, state)

    assert [r["id"] for r in rows] == ["$1", "$2"]
    assert rows[0]["room_name"] == "Dev"
    assert rows[0]["text"].startswith("@alice:matrix.test in Dev at 1970-01-01T00:00:01")
    assert rows[0]["text"].endswith(": hello")
    assert rows[0]["_deleted"] is False
    assert state["since"] == "s1"
    assert state["room_event_ids"][ROOM] == ["$1", "$2"]
    assert "since" not in session.calls[0][1]


def test_incremental_sync_sends_since_and_yields_only_new_events():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "old")])]), state)

    session = FakeMatrixSession([_sync("s2", [_msg("$3", 3000, "new")])])
    rows = _run(session, state)

    assert session.calls[0][1]["since"] == "s1"
    assert [r["id"] for r in rows] == ["$3"]
    assert state["since"] == "s2"
    assert state["room_event_ids"][ROOM] == ["$1", "$3"]


def test_no_new_events_is_a_noop_and_keeps_room_name():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "a")], name="Dev")]), state)
    rows = _run(FakeMatrixSession([_sync("s2")]), state)
    assert rows == []
    assert state["since"] == "s2"
    assert state["room_names"][ROOM] == "Dev"


def test_room_filter_is_pushed_into_the_sync_filter_and_enforced():
    other = "!other:matrix.test"
    payload = _sync("s1", [_msg("$1", 1000, "keep")])
    payload["rooms"]["join"][other] = {"timeline": {"events": [_msg("$x", 1000, "drop")]}}
    session = FakeMatrixSession([payload])

    rows = _run(session, {}, room_ids=[ROOM])

    sent_filter = json.loads(session.calls[0][1]["filter"])
    assert sent_filter["room"]["rooms"] == [ROOM]
    # Synapse drops left rooms from rooms.leave unless the leave event passes the filter.
    assert "m.room.member" in sent_filter["room"]["timeline"]["types"]
    assert [r["id"] for r in rows] == ["$1"]


# ---------------------------------------------------------------------------
# Gap handling
# ---------------------------------------------------------------------------
def test_limited_timeline_is_backfilled_until_known_history():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "seen")])]), state)

    session = FakeMatrixSession(
        [_sync("s2", [_msg("$5", 5000, "e")], limited=True, prev_batch="p1")],
        messages={
            # newest-first, as /messages dir=b returns them
            "p1": {"chunk": [_msg("$4", 4000, "d"), _msg("$3", 3000, "c")], "end": "p2"},
            "p2": {"chunk": [_msg("$2", 2000, "b"), _msg("$1", 1000, "seen")], "end": "p3"},
        },
    )
    rows = _run(session, state)

    assert [r["id"] for r in rows] == ["$2", "$3", "$4", "$5"]
    message_calls = [c for c in session.calls if c[0].endswith("/messages")]
    assert len(message_calls) == 2  # stopped at $1 instead of walking all history


def test_backfill_keeps_same_millisecond_events_and_stops_at_known_id():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "seen")])]), state)

    session = FakeMatrixSession(
        [_sync("s2", [_msg("$3", 2000, "c")], limited=True, prev_batch="p1")],
        messages={"p1": {"chunk": [_msg("$2", 1000, "same ms"), _msg("$1", 1000, "seen")]}},
    )
    rows = _run(session, state)
    assert [r["id"] for r in rows] == ["$2", "$3"]


def test_backfill_respects_max_events_on_first_run():
    session = FakeMatrixSession(
        [_sync("s1", [_msg("$9", 9000, "latest")], limited=True, prev_batch="p1")],
        messages={
            "p1": {"chunk": [_msg(f"${i}", i * 1000, str(i)) for i in range(8, 0, -1)], "end": "p2"}
        },
    )
    rows = _run(session, {}, max_backfill_events=3)
    assert [r["id"] for r in rows] == ["$6", "$7", "$8", "$9"]


# ---------------------------------------------------------------------------
# Redactions, edits, skipped events, threads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("v11", [False, True])
def test_redaction_emits_hard_delete_marker(v11):
    state = {}
    _run(
        FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "oops"), _msg("$2", 1500, "ok")])]), state
    )

    rows = _run(FakeMatrixSession([_sync("s2", [_redaction("$r", 2000, "$1", v11=v11)])]), state)

    assert rows == [{"id": "$1", "_deleted": True}]
    assert state["room_event_ids"][ROOM] == ["$2"]


def test_message_redacted_within_the_same_batch_is_never_ingested():
    rows = _run(
        FakeMatrixSession(
            [_sync("s1", [_msg("$1", 1000, "secret"), _redaction("$r", 1100, "$1")])]
        ),
        {},
    )
    assert rows == [{"id": "$1", "_deleted": True}]


def test_already_redacted_event_becomes_delete_marker():
    redacted = {
        "type": "m.room.message",
        "event_id": "$1",
        "sender": "@alice:matrix.test",
        "origin_server_ts": 1000,
        "content": {},
        "unsigned": {"redacted_because": {"type": "m.room.redaction"}},
    }
    rows = _run(FakeMatrixSession([_sync("s1", [redacted])]), {})
    assert rows == [{"id": "$1", "_deleted": True}]


def test_edit_updates_original_row_in_same_batch():
    rows = _run(
        FakeMatrixSession(
            [_sync("s1", [_msg("$1", 1000, "teh plan"), _edit("$e", 2000, "$1", "the plan")])]
        ),
        {},
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "$1"
    assert rows[0]["text"].endswith(": the plan")
    assert rows[0]["sent_at"].startswith("1970-01-01T00:00:01")


def test_edit_from_later_run_upserts_original_id():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "v1")])]), state)
    rows = _run(FakeMatrixSession([_sync("s2", [_edit("$e", 2000, "$1", "v2")])]), state)
    assert [r["id"] for r in rows] == ["$1"]
    assert rows[0]["text"].endswith(": v2")


def test_edit_from_another_sender_is_ignored():
    rows = _run(
        FakeMatrixSession(
            [
                _sync(
                    "s1",
                    [_msg("$1", 1000, "mine"), _edit("$e", 2000, "$1", "hijack", sender="@eve:x")],
                )
            ]
        ),
        {},
    )
    assert rows[0]["text"].endswith(": mine")


def test_encrypted_and_non_text_events_are_skipped():
    encrypted = {"type": "m.room.encrypted", "event_id": "$enc", "origin_server_ts": 1000}
    image = _msg("$img", 1100, "cat.png", msgtype="m.image")
    rows = _run(FakeMatrixSession([_sync("s1", [encrypted, image, _msg("$1", 1200, "hi")])]), {})
    assert [r["id"] for r in rows] == ["$1"]


def test_thread_root_and_reply_are_captured():
    reply = _msg(
        "$2",
        2000,
        "in thread",
        relates={
            "rel_type": "m.thread",
            "event_id": "$1",
            "m.in_reply_to": {"event_id": "$1"},
        },
    )
    rows = _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "root"), reply])]), {})
    assert rows[1]["thread_root"] == "$1"
    assert rows[1]["reply_to"] == "$1"
    assert rows[0]["thread_root"] is None


def test_leaving_a_room_forgets_its_messages():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "a"), _msg("$2", 2000, "b")])]), state)

    rows = _run(FakeMatrixSession([_sync("s2", leave={ROOM: {}})]), state)

    assert rows == [{"id": "$1", "_deleted": True}, {"id": "$2", "_deleted": True}]
    assert ROOM not in state["room_event_ids"]


def test_edit_arriving_before_original_in_same_batch_keeps_new_text():
    # Backfill /messages can surface an edit ahead of the original event.
    rows = _run(
        FakeMatrixSession(
            [_sync("s1", [_edit("$e", 500, "$1", "the plan"), _msg("$1", 1000, "teh plan")])]
        ),
        {},
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "$1"
    assert rows[0]["text"].endswith(": the plan")
    assert rows[0]["sent_at"].startswith("1970-01-01T00:00:01")


def test_edit_does_not_resurrect_a_previously_redacted_message():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "secret")])]), state)
    _run(FakeMatrixSession([_sync("s2", [_redaction("$r", 2000, "$1")])]), state)

    rows = _run(FakeMatrixSession([_sync("s3", [_edit("$e", 3000, "$1", "resurrect")])]), state)

    assert rows == []
    assert "$1" in state["redacted_ids"]
    assert "$1" not in state["room_event_ids"].get(ROOM, [])


def test_forget_on_leave_false_keeps_messages_after_leave():
    state = {}
    _run(FakeMatrixSession([_sync("s1", [_msg("$1", 1000, "a"), _msg("$2", 2000, "b")])]), state)

    rows = _run(
        FakeMatrixSession([_sync("s2", leave={ROOM: {}})]),
        state,
        forget_on_leave=False,
    )

    assert rows == []
    assert ROOM not in state["room_event_ids"]
    assert ROOM not in state["room_last_ts"]


# ---------------------------------------------------------------------------
# dlt wiring
# ---------------------------------------------------------------------------
def test_matrix_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")
    resource = matrix_source(homeserver=HS, session=FakeMatrixSession([]))
    assert resource.name == "matrix_messages"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_matrix_source_requires_token_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="access_token"):
        matrix_source(homeserver=HS)


def test_forget_on_redaction_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_matrix_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "matrix.duckdb")),
        dataset_name="chat",
    )
    session = FakeMatrixSession(
        [
            _sync("s1", [_msg("$1", 1000, "keep"), _msg("$2", 2000, "remove me")]),
            _sync("s2", [_redaction("$r", 3000, "$2")]),
        ]
    )
    pipeline.run(matrix_source(homeserver=HS, session=session))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM matrix_messages")[0][0] == 2

    # Second run reuses the cursor persisted in dlt state and applies the redaction.
    pipeline.run(matrix_source(homeserver=HS, session=session))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM matrix_messages")
    assert [r[0] for r in rows] == ["$1"]
    assert session.calls[1][1]["since"] == "s1"
