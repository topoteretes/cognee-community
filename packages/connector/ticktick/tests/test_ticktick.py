"""Unit tests for the TickTick dlt connector.

Two layers, all runnable in CI without a live TickTick token:

* DB-free tests for task/project rendering, inbox id resolution, completed-task
  window bisection, and snapshot scoping.
* dlt-pipeline tests (mocked HTTP session, temp sqlite destination) covering
  the acceptance criteria: re-sync reflects edits, and deleted/vanished items
  drop out of the full-snapshot load (forget-on-delete).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest

from cognee_community_connector_ticktick.ticktick import (
    _COMPLETED_PAGE_LIMIT,
    TICKTICK_SOURCE_NAME,
    TickTickAuthError,
    TickTickSnapshotError,
    _fetch_completed_safe,
    _format_ticktick_time,
    _midpoint,
    _parse_ticktick_time,
    _render_project,
    _render_task,
    _resolve_inbox_id,
    build_snapshot,
    ticktick_source,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _project(project_id: str, name: str, *, kind: str = "TASK", view_mode: str = "list") -> dict:
    return {
        "id": project_id,
        "name": name,
        "kind": kind,
        "viewMode": view_mode,
        "closed": False,
    }


def _task(
    task_id: str,
    *,
    project_id: str,
    title: str,
    content: str = "",
    desc: str = "",
    kind: str = "TEXT",
    status: int = 0,
    priority: int = 0,
    tags: list[str] | None = None,
    items: list[dict] | None = None,
    modified_time: str = "2024-01-01T00:00:00+0000",
    sort_order: int = 0,
    etag: str = "etag1",
) -> dict:
    return {
        "id": task_id,
        "projectId": project_id,
        "title": title,
        "content": content,
        "desc": desc,
        "kind": kind,
        "status": status,
        "priority": priority,
        "tags": tags or [],
        "items": items or [],
        "modifiedTime": modified_time,
        "sortOrder": sort_order,
        "etag": etag,
        "startDate": "2024-01-01T00:00:00+0000",
        "dueDate": "2024-01-02T00:00:00+0000",
        "completedTime": "2024-01-03T00:00:00+0000" if status == 2 else None,
    }


class _Resp:
    def __init__(self, payload: Any, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.content = b"x" if payload not in (None, {}) else b""

    def json(self):
        return self._payload


class FakeTickTickSession:
    """Minimal stand-in for a ``requests.Session`` hitting TickTick Open API."""

    def __init__(
        self,
        projects: list[dict],
        tasks_by_project: dict[str, list[dict]],
        completed_tasks: list[dict] | None = None,
        *,
        completed_cap_override: int | None = None,
    ):
        self.projects = projects
        self.tasks_by_project = {k: list(v) for k, v in tasks_by_project.items()}
        self.completed_tasks = list(completed_tasks or [])
        self.completed_cap_override = completed_cap_override
        self.calls: list[tuple[str, str, dict | None]] = []
        self._fail_status: int | None = None

    def fail_with(self, status: int) -> None:
        self._fail_status = status

    def request(
        self,
        method: str,
        url: str,
        params=None,
        json=None,
        timeout=None,
    ):
        if self._fail_status is not None:
            return _Resp({}, status=self._fail_status)

        path = urlparse(url).path
        if path.startswith("/open/v1"):
            path = path[len("/open/v1") :]
        self.calls.append((method.upper(), path, json))

        if method.upper() == "GET" and path == "/project":
            return _Resp(self.projects)

        if method.upper() == "GET" and path.endswith("/data"):
            # /project/{id}/data or /project/inbox/data
            parts = path.strip("/").split("/")
            # ["project", "{id}", "data"]
            project_id = parts[1] if len(parts) >= 3 else ""
            tasks = self.tasks_by_project.get(project_id, [])
            if project_id == "inbox":
                return _Resp({"tasks": tasks, "columns": []})
            project = next((p for p in self.projects if p["id"] == project_id), None)
            return _Resp({"project": project, "tasks": tasks, "columns": []})

        if method.upper() == "POST" and path == "/task/completed":
            body = json or {}
            project_ids = set(body.get("projectIds") or [])
            start = body.get("startDate") or ""
            end = body.get("endDate") or ""
            matching = [
                t
                for t in self.completed_tasks
                if t.get("projectId") in project_ids
                and (not start or (t.get("completedTime") or "") >= start)
                and (not end or (t.get("completedTime") or "") <= end)
            ]
            # Simulate the 200-cap: return at most COMPLETED_PAGE_LIMIT items.
            # When completed_cap_override is set, return exactly that many
            # synthetic rows to force bisection behaviour.
            if self.completed_cap_override is not None:
                # Return cap many clones so _fetch_completed_safe sees a full page.
                base = (
                    matching[0]
                    if matching
                    else _task(
                        "cap", project_id=next(iter(project_ids), "p1"), title="cap", status=2
                    )
                )
                matching = [
                    {**base, "id": f"{base.get('id', 'cap')}-{i}"}
                    for i in range(self.completed_cap_override)
                ]
            return _Resp(matching[:_COMPLETED_PAGE_LIMIT])

        raise AssertionError(f"unexpected request: {method} {path}")


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_render_task_text_kind():
    task = _task(
        "t1",
        project_id="p1",
        title="Buy milk",
        content="2% milk",
        kind="TEXT",
        priority=3,
        tags=["errands"],
    )
    row = _render_task(task, "Groceries")

    assert row["id"] == "task:t1"
    assert row["title"] == "Buy milk"
    assert "Status: Open" in row["content"]
    assert "Priority: Medium" in row["content"]
    assert "Project: Groceries" in row["content"]
    assert "Tags: errands" in row["content"]
    assert "Kind: TEXT" in row["content"]
    assert "2% milk" in row["content"]


def test_render_task_checklist_kind():
    task = _task(
        "t2",
        project_id="p1",
        title="Pack",
        kind="CHECKLIST",
        desc="Trip checklist",
        items=[
            {"id": "i2", "title": "Passport", "status": 1, "sortOrder": 2},
            {"id": "i1", "title": "Tickets", "status": 0, "sortOrder": 1},
        ],
    )
    row = _render_task(task, "Travel")

    assert row["title"] == "Pack"
    assert "Kind: CHECKLIST" in row["content"]
    assert "Trip checklist" in row["content"]
    assert "- [ ] Tickets" in row["content"]
    assert "- [x] Passport" in row["content"]
    # Checklist order follows sortOrder.
    assert row["content"].index("Tickets") < row["content"].index("Passport")


def test_render_task_note_kind():
    task = _task(
        "t3",
        project_id="p2",
        title="Meeting notes",
        kind="NOTE",
        content="Discussed Q3 roadmap",
        desc="From standup",
    )
    row = _render_task(task, "Notes")

    assert "Kind: NOTE" in row["content"]
    assert "Discussed Q3 roadmap" in row["content"]
    assert "From standup" in row["content"]


def test_render_task_excludes_volatile_fields():
    task = _task(
        "t4",
        project_id="p1",
        title="Stable",
        modified_time="2024-06-01T00:00:00+0000",
        sort_order=999,
        etag="changed",
    )
    row = _render_task(task, "Work")

    # Only identity + prose — no volatile timestamps / order / etag.
    assert "modifiedTime" not in row
    assert "sortOrder" not in row
    assert "etag" not in row
    assert "2024-06-01" not in row["content"]
    assert "999" not in row["content"]
    assert "changed" not in row["content"]
    assert set(row.keys()) == {"id", "title", "content", "url"}


def test_render_project():
    project = _project("p1", "Work", kind="NOTE", view_mode="kanban")
    row = _render_project(project)

    assert row["id"] == "project:p1"
    assert row["title"] == "Work"
    assert "Kind: NOTE" in row["content"]
    assert "View: kanban" in row["content"]


def test_inbox_id_resolution():
    tasks = [
        _task("a", project_id="p1", title="x"),
        _task("b", project_id="inbox115781412", title="y"),
    ]
    assert _resolve_inbox_id(tasks) == "inbox115781412"
    assert _resolve_inbox_id([_task("a", project_id="p1", title="x")]) is None


# ---------------------------------------------------------------------------
# Time helpers / completed-task bisection
# ---------------------------------------------------------------------------


def test_midpoint_and_parse_roundtrip():
    start = "2024-01-01T00:00:00+0000"
    end = "2024-01-03T00:00:00+0000"
    mid = _midpoint(start, end)
    assert mid == "2024-01-02T00:00:00+0000"
    assert _format_ticktick_time(_parse_ticktick_time(mid)) == mid


def test_completed_window_bisection(monkeypatch):
    """A full 200-cap response triggers recursive window splits."""
    session = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={"p1": []},
        completed_tasks=[
            _task(
                "c1",
                project_id="p1",
                title="done-early",
                status=2,
            ),
        ],
    )
    # Force the first call (wide window) to look full, then narrower windows
    # to return real matching tasks.
    call_count = {"n": 0}
    real_get = session.request

    def capped_request(method, url, params=None, json=None, timeout=None):
        path = urlparse(url).path
        if path.endswith("/task/completed") or path.endswith("task/completed"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First (wide) window: pretend we hit the cap.
                session.completed_cap_override = _COMPLETED_PAGE_LIMIT
            else:
                session.completed_cap_override = None
        return real_get(method, url, params=params, json=json, timeout=timeout)

    session.request = capped_request  # type: ignore[method-assign]

    # Patch completedTime so the narrow windows still match.
    session.completed_tasks[0]["completedTime"] = "2024-01-02T12:00:00+0000"
    tasks = _fetch_completed_safe(
        session,
        ["p1"],
        "2024-01-01T00:00:00+0000",
        "2024-01-03T00:00:00+0000",
    )
    assert call_count["n"] >= 3  # 1 full + left + right
    assert any(t["id"].startswith("c1") or t["title"] == "done-early" for t in tasks)


def test_unsplittable_window_aborts():
    session = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={"p1": []},
        completed_tasks=[
            _task("c1", project_id="p1", title="done", status=2),
        ],
        completed_cap_override=_COMPLETED_PAGE_LIMIT,
    )
    # Same second start/end → midpoint cannot narrow.
    with pytest.raises(TickTickSnapshotError, match="cannot be narrowed"):
        _fetch_completed_safe(
            session,
            ["p1"],
            "2024-01-01T00:00:00+0000",
            "2024-01-01T00:00:00+0000",
        )


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def test_build_snapshot_all_projects():
    session = FakeTickTickSession(
        projects=[
            _project("p1", "Work"),
            _project("p2", "Personal", kind="NOTE"),
        ],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Ship connector")],
            "p2": [_task("t2", project_id="p2", title="Journal", kind="NOTE")],
            "inbox": [
                _task("t3", project_id="inbox115781412", title="Quick capture"),
            ],
        },
        completed_tasks=[],
    )
    rows = build_snapshot(session, include_completed=False)
    ids = {r["id"] for r in rows}

    assert "project:p1" in ids
    assert "project:p2" in ids
    assert "task:t1" in ids
    assert "task:t2" in ids
    assert "task:t3" in ids
    # Inbox itself is not a project row (API has no Inbox project object).
    assert "project:inbox" not in ids
    assert "project:inbox115781412" not in ids


def test_build_snapshot_selected_projects():
    session = FakeTickTickSession(
        projects=[
            _project("p1", "Work"),
            _project("p2", "Personal"),
        ],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Work task")],
            "p2": [_task("t2", project_id="p2", title="Personal task")],
            "inbox": [_task("t3", project_id="inbox1", title="Inbox task")],
        },
    )
    rows = build_snapshot(
        session,
        selected_project_ids=["p1", "inbox"],
        include_completed=False,
    )
    ids = {r["id"] for r in rows}

    assert "project:p1" in ids
    assert "task:t1" in ids
    assert "task:t3" in ids
    assert "project:p2" not in ids
    assert "task:t2" not in ids

    # Only the selected project + inbox were fetched.
    data_calls = [c for c in session.calls if c[0] == "GET" and c[1].endswith("/data")]
    paths = {c[1] for c in data_calls}
    assert "/project/p1/data" in paths
    assert "/project/inbox/data" in paths
    assert "/project/p2/data" not in paths


def test_completed_tasks_included():
    session = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Open")],
            "inbox": [],
        },
        completed_tasks=[
            _task(
                "c1",
                project_id="p1",
                title="Done",
                status=2,
                content="finished",
            ),
        ],
    )
    # Stamp completedTime inside the default 90-day window.
    session.completed_tasks[0]["completedTime"] = _format_ticktick_time(
        _parse_ticktick_time("2099-01-01T00:00:00+0000")  # far future won't match
    )
    # Use a completedTime that will match whatever "now" window the snapshot uses:
    # set completedTime to a recent timestamp relative to the window by using
    # a very large completed_since_days in the call below instead.
    session.completed_tasks[0]["completedTime"] = "2020-06-01T00:00:00+0000"

    rows = build_snapshot(session, include_completed=True, completed_since_days=36500)
    ids = {r["id"] for r in rows}
    assert "task:t1" in ids
    assert "task:c1" in ids
    done = next(r for r in rows if r["id"] == "task:c1")
    assert "Status: Completed" in done["content"]
    assert "finished" in done["content"]


def test_empty_snapshot_returns_empty_list():
    """An empty account is a valid empty snapshot (not an abort)."""
    session = FakeTickTickSession(projects=[], tasks_by_project={"inbox": []}, completed_tasks=[])
    rows = build_snapshot(session, include_completed=False)
    assert rows == []


# ---------------------------------------------------------------------------
# ticktick_source wiring
# ---------------------------------------------------------------------------


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = ticktick_source(session=FakeTickTickSession([], {"inbox": []}))
    assert TICKTICK_SOURCE_NAME == "ticktick"
    assert document_source_tag(source) == "ticktick"


def test_source_requires_credentials():
    with pytest.raises(ValueError, match="access_token"):
        ticktick_source()


def test_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="dlt"):
        ticktick_source(session=object())


def test_auth_error_raises():
    session = FakeTickTickSession([_project("p1", "Work")], {"p1": [], "inbox": []})
    session.fail_with(401)
    with pytest.raises(TickTickAuthError):
        build_snapshot(session, include_completed=False)


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, session, **kwargs):
    """Run ticktick_source through a dlt pipeline into a temp sqlite destination."""
    db_path = (tmp_path / "ticktick.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="ticktick_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="ticktick_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(ticktick_source(session=session, **kwargs))
    return pipeline


def _read_items(pipeline):
    """Return {id: row-dict} for the ticktick_items table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM ticktick_items") as cursor,
    ):
        rows = cursor.fetchall()
        return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_items(dlt_mod, tmp_path):
    session = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Alpha", content="alpha body")],
            "inbox": [],
        },
        completed_tasks=[],
    )
    pipeline = _run_sync(dlt_mod, tmp_path, session, include_completed=False)
    rows = _read_items(pipeline)

    assert "project:p1" in rows
    assert "task:t1" in rows
    assert "alpha body" in rows["task:t1"]["content"]


def test_edit_reflected_on_resync(dlt_mod, tmp_path):
    session1 = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Alpha", content="v1")],
            "inbox": [],
        },
    )
    _run_sync(dlt_mod, tmp_path, session1, include_completed=False)

    session2 = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Alpha", content="v2")],
            "inbox": [],
        },
    )
    pipeline = _run_sync(dlt_mod, tmp_path, session2, include_completed=False)
    rows = _read_items(pipeline)

    assert "v2" in rows["task:t1"]["content"]
    assert "v1" not in rows["task:t1"]["content"]


def test_deleted_item_removed_on_resync(dlt_mod, tmp_path):
    session1 = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [
                _task("t1", project_id="p1", title="Alpha"),
                _task("t2", project_id="p1", title="Beta"),
            ],
            "inbox": [],
        },
    )
    _run_sync(dlt_mod, tmp_path, session1, include_completed=False)

    # t1 deleted upstream — absent from the listing.
    session2 = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t2", project_id="p1", title="Beta")],
            "inbox": [],
        },
    )
    pipeline = _run_sync(dlt_mod, tmp_path, session2, include_completed=False)
    rows = _read_items(pipeline)

    assert "task:t1" not in rows
    assert "task:t2" in rows


def test_noop_rerun(dlt_mod, tmp_path):
    session = FakeTickTickSession(
        projects=[_project("p1", "Work")],
        tasks_by_project={
            "p1": [_task("t1", project_id="p1", title="Alpha", content="same")],
            "inbox": [],
        },
    )
    pipeline1 = _run_sync(dlt_mod, tmp_path, session, include_completed=False)
    rows1 = _read_items(pipeline1)

    pipeline2 = _run_sync(dlt_mod, tmp_path, session, include_completed=False)
    rows2 = _read_items(pipeline2)

    assert rows1 == rows2
    assert rows2["task:t1"]["content"] == rows1["task:t1"]["content"]
