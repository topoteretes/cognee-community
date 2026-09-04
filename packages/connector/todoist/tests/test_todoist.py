"""Unit tests for the Todoist dlt connector.

Three layers, all runnable in CI without a live Todoist token:

* DB-free tests for row building, the deletion-feed mapping, timestamp
  parsing, and error classification.
* Sync-strategy tests driving ``_sync`` with an in-memory fake client,
  covering the incremental cursor contract and both deletion paths.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: first-run backfill, incremental re-sync picks up only
  changes, and deleted objects vanish from staging (forget-on-delete).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

import cognee_community_connector_todoist.todoist as todoist_mod
from cognee_community_connector_todoist.todoist import (
    TODOIST_SOURCE_NAME,
    TodoistSyncClient,
    _extract_deleted,
    _is_transient,
    _note_row,
    _parse_ts,
    _project_row,
    _sync,
    _task_row,
    _tombstone_row,
    todoist_source,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _ts(hours_ago: float = 0.0) -> str:
    dt = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _ts_at(minutes: float) -> str:
    """A timestamp NOW + ``minutes`` — for events between sync runs."""
    dt = NOW + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin _utcnow_iso to a clock that advances 10 minutes per sync run.

    Sync runs then happen at 12:10, 12:20, ... and events created with
    ``_ts_at`` can be placed deterministically between them.
    """
    calls = {"n": 0}

    def fake_now():
        calls["n"] += 1
        return (NOW + timedelta(minutes=10 * calls["n"])).isoformat()

    monkeypatch.setattr(todoist_mod, "_utcnow_iso", fake_now)
    return calls


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _project(pid="P1", name="Inbox project", description=""):
    return {"id": pid, "name": name, "description": description}


def _task(tid="T1", content="Buy milk", description="", project_id="P1", is_deleted=0):
    return {
        "id": tid,
        "project_id": project_id,
        "content": content,
        "description": description,
        "priority": 3,
        "due": {"date": "2026-09-10"},
        "is_deleted": is_deleted,
    }


def _note(nid="N1", content="Called the store", item_id="T1", project_id=None, is_deleted=0):
    return {
        "id": nid,
        "content": content,
        "item_id": item_id,
        "project_id": project_id,
        "is_deleted": is_deleted,
    }


def _deleted_event(object_type, object_id, minutes=15.0):
    """A deletion event by default placed after the first sync run (12:10)."""
    return {
        "object_type": object_type,
        "event_type": "deleted",
        "object_id": object_id,
        "event_date": _ts_at(minutes),
    }


class FakeTodoist:
    """Stand-in for TodoistSyncClient backed by in-memory fixtures."""

    def __init__(self, projects=None, items=None, notes=None, deletions=None):
        self.projects = projects or []
        self.items = items or []
        self.notes = notes or []
        self.deletions = deletions or []
        self.sync_calls: list[str] = []
        self.since_seen: list[str | None] = []
        self._token_seq = 0

    def sync(self, sync_token: str) -> dict:
        self.sync_calls.append(sync_token)
        self._token_seq += 1
        return {
            "projects": self.projects,
            "items": self.items,
            "notes": self.notes,
            "sync_token": f"tok-{self._token_seq}",
        }

    def deleted_events(self, since: str | None = None) -> list[dict]:
        self.since_seen.append(since)
        if since is None:
            return list(self.deletions)
        cutoff = _parse_ts(since)
        return [e for e in self.deletions if _parse_ts(e.get("event_date")) >= cutoff]


# ---------------------------------------------------------------------------
# Row building (DB-free)
# ---------------------------------------------------------------------------


def test_project_row_flattens_project():
    row = _project_row(_project(pid="P1", name="Work", description="All work tasks"))
    assert row == {
        "id": "P1",
        "kind": "project",
        "url": "https://app.todoist.com/app/project/P1",
        "title": "Work",
        "content": "All work tasks",
        "_deleted": False,
    }


def test_project_row_falls_back_to_name_when_no_description():
    row = _project_row(_project(pid="P1", name="Work"))
    assert row["content"] == "Work"


def test_task_row_includes_description_and_skips_volatile_fields():
    row = _task_row(_task(tid="T1", content="Buy milk", description="skim, 2 liters"))
    assert row["id"] == "T1"
    assert row["kind"] == "task"
    assert row["url"] == "https://app.todoist.com/app/task/T1"
    assert row["title"] == "Buy milk"
    assert "Buy milk" in row["content"]
    assert "skim, 2 liters" in row["content"]
    # Volatile metadata must not churn the content-hash data_id.
    assert "priority" not in row["content"]
    assert "due" not in row["content"]


def test_note_row_infers_parent_kind():
    assert _note_row(_note(item_id="T1"))["title"] == "Comment on task"
    assert _note_row(_note(item_id=None, project_id="P1"))["title"] == "Comment on project"
    assert _note_row(_note(item_id=None, project_id=None))["title"] == "Comment on item"
    assert _note_row(_note(content="hi"))["content"] == "hi"


def test_tombstone_row_maps_object_kinds():
    assert _tombstone_row("T1", "item")["kind"] == "task"
    assert _tombstone_row("N1", "note")["kind"] == "comment"
    assert _tombstone_row("P1", "project")["kind"] == "project"
    assert _tombstone_row("X1")["kind"] == ""
    tomb = _tombstone_row("T1", "item")
    assert tomb["_deleted"] is True


def test_extract_deleted_keeps_only_tracked_kinds_and_deleted_events():
    events = [
        _deleted_event("item", "T1"),
        _deleted_event("note", "N1"),
        _deleted_event("project", "P1"),
        # updated/completed events are not deletions.
        {"object_type": "item", "event_type": "updated", "object_id": "T2"},
        {"object_type": "reminder", "event_type": "deleted", "object_id": "R1"},
    ]
    rows = list(_extract_deleted(events))
    assert sorted(r["id"] for r in rows) == ["N1", "P1", "T1"]


# ---------------------------------------------------------------------------
# Timestamp / error classification (DB-free)
# ---------------------------------------------------------------------------


def test_parse_ts_handles_both_suffixes():
    assert _parse_ts("2026-09-01T12:00:00.000000Z") == _parse_ts("2026-09-01T12:00:00.000000+00:00")
    assert _parse_ts("garbage") is None
    assert _parse_ts(None) is None


def test_is_transient_classification():
    def status_error(code):
        req = httpx.Request("POST", "https://api.todoist.com/sync/v9/sync")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("err", request=req, response=resp)

    assert _is_transient(status_error(429)) is True
    assert _is_transient(status_error(503)) is True
    assert _is_transient(status_error(401)) is False
    assert _is_transient(httpx.ConnectTimeout("t")) is True
    assert _is_transient(ValueError()) is False


# ---------------------------------------------------------------------------
# Sync strategies (pure, fake client)
# ---------------------------------------------------------------------------


def test_first_sync_uses_full_snapshot_token():
    client = FakeTodoist(items=[_task()])
    state: dict = {}
    rows = list(_sync(client, state))
    assert client.sync_calls == ["*"]
    assert [r["id"] for r in rows if r["kind"] == "task"] == ["T1"]
    assert state["sync_token"] == "tok-1"
    assert state["last_sync_started"]


def test_incremental_sync_reuses_recorded_sync_token():
    client = FakeTodoist(items=[_task()])
    state: dict = {}
    list(_sync(client, state))

    # Second run: the connector must ask for the delta, not a new backfill.
    delta = list(_sync(client, state))
    assert client.sync_calls == ["*", "tok-1"]
    assert delta  # unchanged fixtures still re-yield from the delta payload
    assert state["sync_token"] == "tok-2"


def test_delta_tombstone_marks_rows_deleted():
    client = FakeTodoist(items=[_task(tid="T1", is_deleted=1)])
    rows = list(_sync(client, {}))
    tomb = next(r for r in rows if r["id"] == "T1")
    assert tomb["_deleted"] is True


def test_activities_feed_provides_deletions_with_since_filter(frozen_clock):
    # A deletion at 12:15 (between the two runs) and one at 11:00 (before both).
    deletions = [
        _deleted_event("item", "T1", minutes=15.0),
        _deleted_event("item", "T9", minutes=-60.0),
    ]
    client = FakeTodoist(items=[_task(tid="T1")], deletions=deletions)
    state: dict = {}
    list(_sync(client, state))
    # First run has no baseline: the whole feed is consulted.
    assert client.since_seen[0] is None

    # Second run: only events at/after the first run's sync start are applied.
    rows = list(_sync(client, state))
    assert client.since_seen[1] is not None
    tomb_ids = [r["id"] for r in rows if r.get("_deleted")]
    assert "T1" in tomb_ids  # deleted between the runs
    assert "T9" not in tomb_ids  # predates the baseline, already reconciled


def test_deleted_project_cascades_to_its_tasks(frozen_clock):
    client = FakeTodoist(items=[_task(tid="T1", project_id="P1")])
    state: dict = {}
    list(_sync(client, state))  # full backfill records P1 -> [T1]

    # Second run: the project was deleted upstream between the two runs.
    client2 = FakeTodoist(deletions=[_deleted_event("project", "P1", minutes=15.0)])
    rows = list(_sync(client2, state))

    tomb_ids = [r["id"] for r in rows if r.get("_deleted")]
    assert "P1" in tomb_ids
    assert "T1" in tomb_ids  # cascaded
    assert "P1" not in state["project_tasks"]


def test_deleted_event_for_unknown_object_is_emitted_as_tombstone():
    client = FakeTodoist(deletions=[_deleted_event("item", "T9")])
    rows = list(_sync(client, {}))
    assert any(r["id"] == "T9" and r["_deleted"] for r in rows)


# ---------------------------------------------------------------------------
# Source wiring (DB-free)
# ---------------------------------------------------------------------------


def test_todoist_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = todoist_source(token="test-token")
    assert TODOIST_SOURCE_NAME == "todoist"
    assert document_source_tag(source) == "todoist"


def test_todoist_source_requires_token():
    with pytest.raises(ValueError):
        todoist_source(token=None, client=None)


def test_document_data_item_tags_source():
    # The row -> document-DataItem mapping is owned by the ingestion layer; a
    # Todoist row (with its extra kind/_deleted columns) must map cleanly.
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = SimpleNamespace(
        row_data={
            "id": "T1",
            "kind": "task",
            "url": "https://app.todoist.com/app/task/T1",
            "title": "Buy milk",
            "content": "Buy milk\n\nskim",
            "_deleted": False,
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "T1")

    item = _build_document_data_item(row, data_id, "todoist")

    assert item.external_metadata["source"] == "todoist"
    assert item.external_metadata["external_id"] == "T1"
    assert item.data_id == data_id
    assert item.data.startswith("# Buy milk")


# ---------------------------------------------------------------------------
# dlt pipeline: incremental merge + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _make_pipeline(dlt, tmp_path, name):
    db_path = (tmp_path / f"{name}.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="todoist_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read_rows(pipeline):
    """Return {id: {title, kind, content}} for the todoist staging table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, kind, title, content FROM todoist") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"kind": row[1], "title": row[2], "content": row[3]} for row in rows}


def test_pipeline_backfill_and_incremental_resync(dlt_mod, tmp_path):
    client = FakeTodoist(
        projects=[_project(pid="P1", name="Work")],
        items=[_task(tid="T1", content="Buy milk")],
        notes=[_note(nid="N1", content="Called the store")],
    )
    pipeline = _make_pipeline(dlt_mod, tmp_path, "todoist_resync")
    pipeline.run(todoist_source(client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"P1", "T1", "N1"}
    assert rows["T1"]["title"] == "Buy milk"

    # Incremental run: content changed + one new task, same pipeline (state
    # persists). Only the delta is fetched; merge upserts the changed row.
    client.items = [
        _task(tid="T1", content="Buy oat milk"),
        _task(tid="T2", content="Walk the dog"),
    ]
    pipeline.run(todoist_source(client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"P1", "T1", "T2", "N1"}
    assert "oat milk" in rows["T1"]["content"]
    assert "Buy milk" not in rows["T1"]["content"]
    # Second sync must have used the recorded sync_token, not a fresh backfill.
    assert client.sync_calls == ["*", "tok-1"]


def test_pipeline_delta_tombstone_removes_row(dlt_mod, tmp_path):
    client = FakeTodoist(items=[_task(tid="T1"), _task(tid="T2")])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "todoist_tomb")
    pipeline.run(todoist_source(client=client))
    assert set(_read_rows(pipeline)) == {"T1", "T2"}

    # T1 deleted upstream: the delta returns it as an is_deleted tombstone.
    client.items = [_task(tid="T1", is_deleted=1), _task(tid="T2")]
    pipeline.run(todoist_source(client=client))

    rows = _read_rows(pipeline)
    assert "T1" not in rows
    assert "T2" in rows


def test_pipeline_activities_deletion_removes_row(dlt_mod, tmp_path, frozen_clock):
    client = FakeTodoist(items=[_task(tid="T1"), _task(tid="T2")])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "todoist_feed")
    pipeline.run(todoist_source(client=client))
    assert set(_read_rows(pipeline)) == {"T1", "T2"}

    # T1 deleted upstream between the runs: dropped from the delta entirely;
    # only the activities feed knows about it.
    client.items = [_task(tid="T2")]
    client.deletions = [_deleted_event("item", "T1", minutes=15.0)]
    pipeline.run(todoist_source(client=client))

    rows = _read_rows(pipeline)
    assert "T1" not in rows
    assert "T2" in rows


def test_pipeline_deleted_project_cascades(dlt_mod, tmp_path, frozen_clock):
    client = FakeTodoist(
        projects=[_project(pid="P1")],
        items=[_task(tid="T1", project_id="P1"), _task(tid="T2", project_id="P2")],
    )
    pipeline = _make_pipeline(dlt_mod, tmp_path, "todoist_cascade")
    pipeline.run(todoist_source(client=client))
    assert set(_read_rows(pipeline)) == {"P1", "T1", "T2"}

    # The project (and everything in it) is gone upstream between the runs.
    client.projects = []
    client.items = [_task(tid="T2", project_id="P2")]
    client.deletions = [_deleted_event("project", "P1", minutes=15.0)]
    pipeline.run(todoist_source(client=client))

    rows = _read_rows(pipeline)
    assert "P1" not in rows
    assert "T1" not in rows  # cascaded with its project
    assert "T2" in rows


def test_client_deleted_events_filters_since():
    client = TodoistSyncClient("token")
    # Inject a fake _post instead of hitting the network: two pages, then done.
    pages = [
        {"events": [_deleted_event("item", "T1", minutes=-540.0)] * 100},
        {"events": [_deleted_event("item", "T2", minutes=-30.0)]},
    ]
    calls = []

    def fake_post(url, data):
        calls.append(data)
        return pages[data["offset"] // 100]

    client._post = fake_post

    events = client.deleted_events(since=_ts_at(-60.0))
    assert [e["object_id"] for e in events] == ["T2"]
    # Pagination walked exactly two pages with the documented params.
    assert calls[0]["object_event_type"] == "deleted"
    assert calls[0]["limit"] == 100
