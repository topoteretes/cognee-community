from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from cognee_community_connector_google_chat.google_chat import (
    _iter_rows,
    _messages_from_event,
    _sync_space,
    _thread_to_row,
    google_chat_source,
)


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSpaces:
    def __init__(self, spaces):
        self.space_data = spaces

    def get(self, name):
        return FakeRequest(self.space_data[name])


class FakeMessages:
    def __init__(self, snapshots=None, threads=None):
        self.snapshots = snapshots or {}
        self.threads = threads or {}
        self.calls = []

    def list(self, parent, pageSize=1000, pageToken=None, showDeleted=False, filter=None):
        self.calls.append(
            {
                "parent": parent,
                "pageToken": pageToken,
                "showDeleted": showDeleted,
                "filter": filter,
            }
        )
        key = (parent, filter)
        responses = self.threads.get(key) if filter else self.snapshots.get(parent)
        if responses is None:
            responses = [{}]
        index = 0 if pageToken is None else int(pageToken)
        response = responses[index]
        return FakeRequest(response)


class FakeEvents:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def list(self, parent, filter, pageSize=1000, pageToken=None):
        self.calls.append({"parent": parent, "filter": filter, "pageToken": pageToken})
        responses = self.pages.get(parent, [{}])
        index = 0 if pageToken is None else int(pageToken)
        return FakeRequest(responses[index])


class FakeService:
    def __init__(self, spaces=None, snapshots=None, threads=None, events=None):
        self.space_api = FakeSpaces(spaces or {})
        self.message_api = FakeMessages(snapshots, threads)
        self.event_api = FakeEvents(events)

    def spaces(self):
        return self.space_api

    def messages(self):
        return self.message_api

    def spaceEvents(self):
        return self.event_api


def message(name, thread, text, create_time="2026-01-01T00:00:00Z", **extra):
    return {
        "name": name,
        "thread": {"name": thread},
        "text": text,
        "createTime": create_time,
        "sender": {"name": "users/u1", "displayName": "Ada"},
        **extra,
    }


def test_thread_document_preserves_chronological_context():
    row = _thread_to_row(
        "spaces/s1",
        "spaces/s1/threads/t1",
        [
            message("spaces/s1/messages/m2", "spaces/s1/threads/t1", "Sounds good", "2026-01-01T00:02:00Z"),
            message("spaces/s1/messages/m1", "spaces/s1/threads/t1", "Ship on Friday", "2026-01-01T00:01:00Z"),
        ],
    )

    assert row["id"] == "spaces/s1/threads/t1"
    assert row["space_name"] == "spaces/s1"
    assert row["thread_name"] == "spaces/s1/threads/t1"
    assert row["content"].index("Ship on Friday") < row["content"].index("Sounds good")
    assert "Ada" in row["content"]
    assert row["_deleted"] is False


def test_event_parser_unpacks_singular_and_automatic_batch_events():
    events = [
        {
            "name": "spaces/s1/spaceEvents/e1",
            "eventType": "google.workspace.chat.message.v1.created",
            "eventTime": "2026-01-01T00:00:00Z",
            "messageCreatedEventData": {
                "message": message("spaces/s1/messages/m1", "spaces/s1/threads/t1", "hello")
            },
        },
        {
            "name": "spaces/s1/spaceEvents/e2",
            "eventType": "google.workspace.chat.message.v1.batchUpdated",
            "eventTime": "2026-01-01T00:01:00Z",
            "messageBatchUpdatedEventData": {
                "messages": [
                    {"message": message("spaces/s1/messages/m2", "spaces/s1/threads/t1", "updated")},
                    {"message": message("spaces/s1/messages/m3", "spaces/s1/threads/t2", "another")},
                ]
            },
        },
    ]

    parsed = _messages_from_event(events)

    assert [item["message"]["name"] for item in parsed] == [
        "spaces/s1/messages/m1",
        "spaces/s1/messages/m2",
        "spaces/s1/messages/m3",
    ]
    assert [item["event_type"] for item in parsed] == ["created", "updated", "updated"]


def test_event_parser_handles_all_six_message_event_variants():
    actions = ("created", "updated", "deleted")
    events = []
    for action in actions:
        event_type = f"google.workspace.chat.message.v1.{action}"
        field = f"message{action.title()}EventData"
        events.append(
            {
                "eventType": event_type,
                field: {"message": {"name": f"spaces/s1/messages/{action}"}},
            }
        )
        batch_field = f"messageBatch{action.title()}EventData"
        events.append(
            {
                "eventType": f"google.workspace.chat.message.v1.batch{action.title()}",
                batch_field: {
                    "messages": [{"message": {"name": f"spaces/s1/messages/batch-{action}"}}]
                },
            }
        )

    parsed = _messages_from_event(events)

    assert [item["event_type"] for item in parsed] == [
        "created",
        "created",
        "updated",
        "updated",
        "deleted",
        "deleted",
    ]


def test_initial_backfill_sets_cursor_before_listing_and_groups_threads():
    space = "spaces/s1"
    thread = "spaces/s1/threads/t1"
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE", "displayName": "Team"}},
        snapshots={
            space: [
                {
                    "messages": [
                        message("spaces/s1/messages/m1", thread, "question"),
                    ],
                    "nextPageToken": "1",
                },
                {"messages": [message("spaces/s1/messages/m2", thread, "answer")]},
            ]
        },
    )
    state = {}
    start = datetime(2026, 2, 1, tzinfo=UTC)

    rows = list(_sync_space(service, space, state, now=start))

    assert len(rows) == 1
    assert rows[0]["id"] == thread
    assert "question" in rows[0]["content"] and "answer" in rows[0]["content"]
    assert state[space]["cursor"] == "2026-02-01T00:00:00Z"
    assert set(state[space]["message_threads"]) == {
        "spaces/s1/messages/m1",
        "spaces/s1/messages/m2",
    }
    assert service.message_api.calls[0]["showDeleted"] is True


def test_incremental_batch_update_rebuilds_each_affected_thread_once():
    space = "spaces/s1"
    thread = "spaces/s1/threads/t1"
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        events={
            space: [
                {
                    "spaceEvents": [
                        {
                            "name": "spaces/s1/spaceEvents/e1",
                            "eventTime": "2026-02-02T00:00:01Z",
                            "eventType": "google.workspace.chat.message.v1.batchUpdated",
                            "messageBatchUpdatedEventData": {
                                "messages": [
                                    {"message": message("spaces/s1/messages/m1", thread, "new root")},
                                    {"message": message("spaces/s1/messages/m2", thread, "new reply")},
                                ]
                            },
                        }
                    ]
                }
            ]
        },
        threads={(space, f"thread.name = {thread}"): [{
            "messages": [
                message("spaces/s1/messages/m1", thread, "new root"),
                message("spaces/s1/messages/m2", thread, "new reply"),
            ]
        }]},
    )
    state = {space: {"cursor": "2026-02-01T00:00:00Z", "message_threads": {}}}

    rows = list(_sync_space(service, space, state, now=datetime(2026, 2, 2, 0, 1, tzinfo=UTC)))

    assert len(rows) == 1
    assert rows[0]["id"] == thread
    assert "new root" in rows[0]["content"] and "new reply" in rows[0]["content"]
    assert len(service.message_api.calls) == 1
    assert state[space]["cursor"] == "2026-02-02T00:01:00Z"


def test_deleted_message_uses_saved_thread_mapping_and_tombstones_empty_thread():
    space = "spaces/s1"
    thread = "spaces/s1/threads/t1"
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        events={
            space: [
                {
                    "spaceEvents": [
                        {
                            "name": "spaces/s1/spaceEvents/e1",
                            "eventTime": "2026-02-02T00:00:01Z",
                            "eventType": "google.workspace.chat.message.v1.deleted",
                            "messageDeletedEventData": {
                                "message": {"name": "spaces/s1/messages/m1"}
                            },
                        }
                    ]
                }
            ]
        },
        threads={(space, f"thread.name = {thread}"): [{"messages": []}]},
    )
    state = {
        space: {
            "cursor": "2026-02-01T00:00:00Z",
            "message_threads": {"spaces/s1/messages/m1": thread},
        }
    }

    rows = list(_sync_space(service, space, state, now=datetime(2026, 2, 2, 0, 1, tzinfo=UTC)))

    assert rows == [{"id": thread, "space_name": space, "thread_name": thread, "_deleted": True}]
    assert state[space]["message_threads"] == {}


def test_create_then_delete_in_one_sync_does_not_leave_a_stale_message_mapping():
    space = "spaces/s1"
    thread = "spaces/s1/threads/t1"
    message_name = "spaces/s1/messages/m1"
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        events={
            space: [
                {
                    "spaceEvents": [
                        {
                            "name": "spaces/s1/spaceEvents/e1",
                            "eventTime": "2026-02-02T00:00:01Z",
                            "eventType": "google.workspace.chat.message.v1.created",
                            "messageCreatedEventData": {
                                "message": message(message_name, thread, "transient")
                            },
                        },
                        {
                            "name": "spaces/s1/spaceEvents/e2",
                            "eventTime": "2026-02-02T00:00:02Z",
                            "eventType": "google.workspace.chat.message.v1.deleted",
                            "messageDeletedEventData": {"message": {"name": message_name}},
                        },
                    ]
                }
            ]
        },
        threads={(space, f"thread.name = {thread}"): [{"messages": []}]},
    )
    state = {space: {"cursor": "2026-02-01T00:00:00Z", "message_threads": {}}}

    rows = list(_sync_space(service, space, state, now=datetime(2026, 2, 2, 0, 1, tzinfo=UTC)))

    assert rows == []
    assert state[space]["message_threads"] == {}


def test_expired_cursor_snapshot_reconciles_missing_threads():
    space = "spaces/s1"
    keep = "spaces/s1/threads/keep"
    gone = "spaces/s1/threads/gone"
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        snapshots={
            space: [
                {"messages": [message("spaces/s1/messages/m1", keep, "still here")]},
            ]
        },
    )
    state = {
        space: {
            "cursor": "2026-01-01T00:00:00Z",
            "message_threads": {
                "spaces/s1/messages/m1": keep,
                "spaces/s1/messages/m2": gone,
            },
        }
    }

    rows = list(_sync_space(service, space, state, now=datetime(2026, 2, 2, tzinfo=UTC)))

    assert [row["id"] for row in rows] == [keep, gone]
    assert rows[0]["_deleted"] is False
    assert rows[1]["_deleted"] is True
    assert state[space]["cursor"] == "2026-02-02T00:00:00Z"
    assert state[space]["message_threads"] == {"spaces/s1/messages/m1": keep}


def test_failed_snapshot_page_does_not_advance_or_mutate_space_state():
    space = "spaces/s1"
    original = {
        space: {
            "cursor": None,
            "message_threads": {"spaces/s1/messages/old": "spaces/s1/threads/old"},
        }
    }
    state = deepcopy(original)
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        snapshots={
            space: [
                {"messages": [message("spaces/s1/messages/m1", "spaces/s1/threads/t1", "one")], "nextPageToken": "1"},
                RuntimeError("temporary API failure"),
            ]
        },
    )

    with pytest.raises(RuntimeError, match="temporary API failure"):
        list(_sync_space(service, space, state, now=datetime(2026, 2, 2, tzinfo=UTC)))

    assert state == original


def test_failed_event_page_does_not_advance_or_mutate_space_state():
    space = "spaces/s1"
    original = {space: {"cursor": "2026-02-01T00:00:00Z", "message_threads": {}}}
    state = deepcopy(original)
    service = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE"}},
        events={space: [{"spaceEvents": [], "nextPageToken": "1"}, RuntimeError("event fetch failed")]},
    )

    with pytest.raises(RuntimeError, match="event fetch failed"):
        list(_sync_space(service, space, state, now=datetime(2026, 2, 2, tzinfo=UTC)))

    assert state == original


def test_source_rejects_empty_space_scope_before_building_a_client():
    with pytest.raises(ValueError, match="at least one named space"):
        google_chat_source(space_names=[], service=object())


def test_source_rejects_direct_messages_before_listing_messages():
    space = "spaces/dm"
    service = FakeService(spaces={space: {"name": space, "spaceType": "DIRECT_MESSAGE"}})

    with pytest.raises(ValueError, match="direct messages and group chats"):
        list(_iter_rows(service, [space], {}))

    assert service.message_api.calls == []


def test_removing_a_space_from_configuration_tombstones_its_old_threads():
    selected = "spaces/selected"
    removed = "spaces/removed"
    old_thread = "spaces/removed/threads/old"
    service = FakeService(
        spaces={selected: {"name": selected, "spaceType": "SPACE"}},
        snapshots={selected: [{}]},
    )
    state = {
        removed: {
            "cursor": "2026-02-01T00:00:00Z",
            "message_threads": {"spaces/removed/messages/m1": old_thread},
        }
    }

    rows = list(_iter_rows(service, [selected], state))

    assert rows == [{"id": old_thread, "space_name": removed, "thread_name": old_thread, "_deleted": True}]
    assert removed not in state


def test_failed_selected_space_does_not_emit_tombstones_or_mutate_any_state():
    from datetime import timedelta

    first = "spaces/first"
    second = "spaces/second"
    removed = "spaces/removed"
    service = FakeService(
        spaces={
            first: {"name": first, "spaceType": "SPACE"},
            second: {"name": second, "spaceType": "SPACE"},
        },
        snapshots={first: [{"messages": [message("spaces/first/messages/m1", "spaces/first/threads/t1", "hi")]}]},
        events={second: [RuntimeError("event fetch failed")]},
    )
    cursor = (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    state = {
        first: {"cursor": cursor, "message_threads": {}},
        second: {"cursor": cursor, "message_threads": {}},
        removed: {
            "cursor": cursor,
            "message_threads": {"m1": "spaces/removed/threads/t1"},
        },
    }
    original = deepcopy(state)

    with pytest.raises(RuntimeError, match="event fetch failed"):
        list(_iter_rows(service, [first, second], state))

    assert state == original


def test_dlt_resource_is_document_routed_and_configured_for_hard_delete():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    source = google_chat_source(space_names=["spaces/s1"], service=FakeService())
    resource = source.resources["google_chat_threads"]
    schema = resource.compute_table_schema()

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "google_chat"
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_dlt_merge_updates_thread_and_hard_delete_removes_it(tmp_path):
    import dlt

    space = "spaces/s1"
    thread_a = "spaces/s1/threads/a"
    thread_b = "spaces/s1/threads/b"
    db_path = tmp_path / "chat.db"

    def sync(service):
        pipeline = dlt.pipeline(
            pipeline_name="google_chat_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="google_chat_e2e",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(google_chat_source([space], service=service))
        with pipeline.sql_client() as client:
            return client.execute_sql(
                "SELECT id, content FROM google_chat_threads ORDER BY id"
            )

    backfill = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE", "displayName": "Team"}},
        snapshots={
            space: [
                {
                    "messages": [
                        message("spaces/s1/messages/m1", thread_a, "remove this"),
                        message("spaces/s1/messages/m2", thread_b, "keep this"),
                    ]
                }
            ]
        },
    )
    rows = sync(backfill)
    assert [row[0] for row in rows] == [thread_a, thread_b]

    incremental = FakeService(
        spaces={space: {"name": space, "spaceType": "SPACE", "displayName": "Team"}},
        events={
            space: [
                {
                    "spaceEvents": [
                        {
                            "name": "spaces/s1/spaceEvents/delete-a",
                            "eventTime": "2026-10-21T00:00:00Z",
                            "eventType": "google.workspace.chat.message.v1.deleted",
                            "messageDeletedEventData": {
                                "message": {"name": "spaces/s1/messages/m1"}
                            },
                        }
                    ]
                }
            ]
        },
        threads={(space, f"thread.name = {thread_a}"): [{"messages": []}]},
    )

    rows = sync(incremental)
    assert [row[0] for row in rows] == [thread_b]
