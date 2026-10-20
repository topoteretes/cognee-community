"""Unit tests for the Asana connector.

The Asana REST API is mocked by ``FakeAsana``, an in-memory stand-in for a
``requests`` session, so no token and no network are needed. Its behaviour
mirrors what was observed against a real workspace: ``modified_since`` is
inclusive, an edited comment leaves ``modified_at`` alone and arrives as a
story event with ``parent: null``, ``/events`` answers 412 with a fresh token
when the token is missing or stale, and a bad ``offset`` is a 400.

Three layers:

* pure tests for rendering and for mapping events to tasks;
* ``sync_tasks`` driven with a plain dict as state (ingest, cursor, events,
  deletion, and the failure paths that must not delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination (edit and delete
  across two runs, two sources in one dataset, ``check_active``, a task kept
  for a retry), plus the document-source tag core routes on.
"""

import copy
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_asana import asana
from cognee_community_connector_asana.asana import (
    ASANA_SOURCE_NAME,
    AsanaAPIError,
    AsanaResponseError,
    _documents_for,
    _event_task_gids,
    _get,
    _is_rename,
    _list_all,
    _overlap_start,
    _render_task,
    asana_source,
    sync_tasks,
)

API = "https://app.asana.com/api/1.0"


def day(n):
    """A deterministic Asana-style timestamp, ``day(1) < day(2) < ...``."""
    return f"2026-10-{n:02d}T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Fake Asana REST API
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _error(status, message, headers=None):
    return _Resp(status, {"errors": [{"message": message}]}, headers)


class FakeAsana:
    """Minimal stand-in for a ``requests`` session talking to Asana."""

    def __init__(self, page_size=100):
        self.page_size = page_size
        self.projects = {}  # gid -> {"name", "notes"}
        self.tasks = {}  # gid -> task dict (see add_task)
        self.events = {}  # project gid -> events not yet delivered
        self.tokens = {}  # project gid -> the sync token Asana currently accepts
        self.calls = []  # (path, params) of every request
        self._queued = []  # (path, predicate, response) answered once, in order
        self._token_counter = 0
        self.events_page_size = 100  # events per /events page; more sets has_more

    # -- fixture builders ----------------------------------------------------
    def add_project(self, gid, name="Website launch", notes="Ship the new site."):
        self.projects[gid] = {"name": name, "notes": notes}
        self.events.setdefault(gid, [])

    def add_task(self, gid, *, modified_at, projects=("P1",), parent=None, **fields):
        self.tasks[gid] = {
            "name": f"Task {gid}",
            "notes": "",
            "completed": False,
            "comments": [],  # [(author, text)]
            **fields,
            "modified_at": modified_at,
            "projects": list(projects) if parent is None else [],
            "parent": parent,
        }

    def emit(self, project_gid, **event):
        self.events[project_gid].append(event)

    def queue(self, path, response, when=lambda params: True):
        """Answer the next matching request with ``response`` instead of real data."""
        self._queued.append((path, when, response))

    def expire_token(self, project_gid):
        self.tokens[project_gid] = self._new_token(project_gid)

    def requested(self, path):
        return [params for called, params in self.calls if called == path]

    # -- requests.Session surface ----------------------------------------------
    def get(self, url, params=None, timeout=None):
        assert url.startswith(API + "/"), f"unexpected URL: {url}"
        assert timeout, "every request must carry a timeout"
        path, params = url[len(API) :], dict(params or {})
        self.calls.append((path, params))
        for index, (queued_path, when, response) in enumerate(self._queued):
            if queued_path == path and when(params):
                del self._queued[index]
                return response
        return self._route(path, params)

    def _route(self, path, params):
        parts = path.strip("/").split("/")
        if parts == ["events"]:
            return self._events(params)
        if parts == ["projects"]:
            return self._page([{"gid": gid} for gid in self.projects], params)
        if parts[0] == "projects" and len(parts) == 2:
            project = self.projects.get(parts[1])
            if project is None:
                return _error(404, "project: Unknown object")
            data = {
                "gid": parts[1],
                **project,
                "permalink_url": f"https://app.asana.com/p/{parts[1]}",
            }
            return _Resp(200, {"data": data})
        if parts == ["tasks"]:
            return self._list_tasks(params)
        if parts[0] == "tasks" and parts[1] not in self.tasks:
            return _error(404, "task: Unknown object")
        if parts[0] == "tasks" and len(parts) == 2:
            return _Resp(200, {"data": self._task_detail(parts[1])})
        if parts[0] == "tasks" and parts[2] == "stories":
            return self._page(self._stories(parts[1]), params)
        if parts[0] == "tasks" and parts[2] == "subtasks":
            subtasks = [
                {
                    "gid": gid,
                    "name": task["name"],
                    "completed": task["completed"],
                    "assignee": {"name": task["assignee"]} if task.get("assignee") else None,
                    "due_on": task.get("due_on"),
                    "notes": task["notes"],
                }
                for gid, task in self.tasks.items()
                if task["parent"] == parts[1]
            ]
            return self._page(subtasks, params)
        raise AssertionError(f"unexpected path: {path}")

    # -- endpoints -------------------------------------------------------------
    def _new_token(self, project_gid):
        self._token_counter += 1
        return f"sync-{project_gid}-{self._token_counter}"

    def _events(self, params):
        project_gid = params["resource"]
        assert "resource.target.gid" in params["opt_fields"], "edited comments need the target"
        assert "change.field" in params["opt_fields"], "renames are told apart by change.field"
        fresh = self._new_token(project_gid)
        if params.get("sync") is None or params["sync"] != self.tokens.get(project_gid):
            # Missing or stale token: events so far are lost, a new token is issued.
            self.tokens[project_gid] = fresh
            self.events[project_gid] = []
            return _Resp(
                412, {"errors": [{"message": "Sync token invalid or too old."}], "sync": fresh}
            )
        pending = self.events[project_gid]
        delivered, self.events[project_gid] = (
            pending[: self.events_page_size],
            pending[self.events_page_size :],
        )
        self.tokens[project_gid] = fresh
        has_more = bool(self.events[project_gid])
        return _Resp(200, {"data": delivered, "sync": fresh, "has_more": has_more})

    def _list_tasks(self, params):
        assert params.get("project") in self.projects, "listing needs a known project"
        fields = params.get("opt_fields", "")
        since = params.get("modified_since")
        items = []
        for gid, task in self.tasks.items():
            if params["project"] not in task["projects"]:
                continue
            if since and task["modified_at"] < since:  # inclusive, as observed live
                continue
            if params.get("completed_since") == "now" and task["completed"]:
                continue
            item = {"gid": gid}
            if "modified_at" in fields:
                item["modified_at"] = task["modified_at"]
            items.append(item)
        return self._page(items, params)

    def _task_detail(self, gid):
        task = self.tasks[gid]
        return {
            "gid": gid,
            "name": task["name"],
            "notes": task["notes"],
            "completed": task["completed"],
            "due_on": task.get("due_on"),
            "permalink_url": f"https://app.asana.com/t/{gid}",
            "assignee": {"name": task["assignee"]} if task.get("assignee") else None,
            "memberships": [
                {
                    "project": {"gid": p, "name": self.projects[p]["name"]},
                    "section": {"name": task.get("section", "To do")},
                }
                for p in task["projects"]
            ],
            "custom_fields": task.get("custom_fields", []),
            "parent": {"gid": task["parent"]} if task["parent"] else None,
        }

    def _stories(self, gid):
        system = {"resource_subtype": "added_to_project", "text": "added this task to a project"}
        comments = [
            {"resource_subtype": "comment_added", "text": text, "created_by": {"name": author}}
            for author, text in self.tasks[gid]["comments"]
        ]
        return [system, *comments]

    def _page(self, items, params):
        limit = min(int(params.get("limit", 20)), self.page_size)
        start = int(params["offset"].split(":")[1]) if params.get("offset") else 0
        end = start + limit
        next_page = {"offset": f"offset:{end}"} if end < len(items) else None
        return _Resp(200, {"data": items[start:end], "next_page": next_page})


# Event payloads shaped as /events returns them with the connector's opt_fields.
def comment_added(story_gid, task_gid):
    return {
        "action": "added",
        "resource": {"gid": story_gid, "resource_type": "story", "target": _task_ref(task_gid)},
        "parent": _task_ref(task_gid),
    }


def comment_edited(story_gid, task_gid):
    # parent is null on an edit; only resource.target names the task.
    return {
        "action": "changed",
        "resource": {"gid": story_gid, "resource_type": "story", "target": _task_ref(task_gid)},
        "parent": None,
    }


def task_changed(task_gid):
    return {
        "action": "changed",
        "resource": {"gid": task_gid, "resource_type": "task"},
        "parent": None,
    }


def _task_ref(gid):
    return {"gid": gid, "resource_type": "task"}


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry waits instead of sleeping."""
    recorded = []
    monkeypatch.setattr(asana.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def fake():
    session = FakeAsana()
    session.add_project("P1")
    return session


# The run clock for sync_tasks: after every timestamp the tests use.
NOW = datetime(2026, 10, 28, tzinfo=UTC)


def hours(n):
    """The run clock ``n`` hours after ``NOW``, for the give-up rule's 24 hours."""
    return NOW + timedelta(hours=n)


def run_sync(session, state, project_gids=("P1",), now=NOW, **kwargs):
    return list(sync_tasks(session, state, project_gids=list(project_gids), now=now, **kwargs))


def task_rows(rows):
    return {r["id"]: r for r in rows if r["id"].startswith("task:") and not r["_deleted"]}


def tombstones(rows):
    return [r for r in rows if r["_deleted"]]


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def test_render_task_includes_fields_subtasks_and_comments():
    task = {
        "name": "Write launch post",
        "notes": "Draft, then review with marketing.\n",
        "completed": False,
        "due_on": "2026-10-20",
        "assignee": {"name": "Ada"},
        "memberships": [
            {"project": {"name": "Website launch"}, "section": {"name": "In progress"}},
            {"project": {"name": "Blog"}, "section": None},
        ],
        "custom_fields": [
            {"name": "Priority", "display_value": "High"},
            {"name": "Estimate", "display_value": None},  # unset fields are left out
        ],
    }
    comments = [{"text": "Looks good", "created_by": {"name": "Grace"}}, {"text": "Shipping"}]
    subtasks = [
        {"name": "Outline", "completed": True, "assignee": None, "due_on": None, "notes": ""},
        {
            "name": "Screenshots",
            "completed": False,
            "assignee": {"name": "Grace"},
            "due_on": "2026-10-18",
            "notes": "Desktop and mobile.\nLight theme only.\n",
        },
    ]

    assert _render_task(task, comments, subtasks) == (
        "Completed: no\n"
        "Assignee: Ada\n"
        "Due: 2026-10-20\n"
        "Project: Blog\n"
        "Project: Website launch (section: In progress)\n"
        "Priority: High\n"
        "\n"
        "Draft, then review with marketing.\n"
        "\n"
        "Subtasks:\n"
        "- [x] Outline\n"  # nothing is printed for unset details
        "- [ ] Screenshots\n"
        "  Assignee: Grace\n"
        "  Due: 2026-10-18\n"
        "  Notes: Desktop and mobile.\n"
        "  Light theme only.\n"
        "\n"
        "Comments:\n"
        "- Grace: Looks good\n"
        "- Unknown: Shipping"
    )


def test_shuffled_lists_render_the_same_text():
    # The same task with its memberships, custom fields, subtasks and comments
    # handed over in a different order.
    task = {
        "completed": False,
        "memberships": [
            {"project": {"name": name}, "section": {"name": "To do"}} for name in "CAB"
        ],
        "custom_fields": [
            {"name": n, "display_value": v} for n, v in zip("ZXY", "123", strict=True)
        ],
    }
    subtasks = [{"gid": str(gid), "name": f"Step {gid}", "completed": False} for gid in (9, 10, 11)]
    comments = [
        {"gid": str(100 + i), "text": f"comment {i}", "created_at": day(i + 1)} for i in range(4)
    ]
    expected = _render_task(task, comments, subtasks)

    rng = random.Random(7)
    for _ in range(5):
        shuffled = {
            **task,
            "memberships": rng.sample(task["memberships"], 3),
            "custom_fields": rng.sample(task["custom_fields"], 3),
        }
        assert _render_task(shuffled, rng.sample(comments, 4), rng.sample(subtasks, 3)) == expected
    # Oldest comment first, subtasks in creation order (gid 9 before 10).
    assert expected.index("comment 0") < expected.index("comment 3")
    assert expected.index("Step 9") < expected.index("Step 10")


def test_render_task_minimal_and_completed():
    assert _render_task({"completed": True}, [], []) == "Completed: yes"


def test_render_task_ignores_volatile_fields():
    # Timestamps and counters must not reach the text, or an unchanged task would
    # get a new content hash and be re-cognified on every sync.
    base = {"name": "T", "notes": "body", "modified_at": day(1), "num_likes": 0}
    later = {**base, "modified_at": day(9), "num_likes": 7}
    assert _render_task(base, [], []) == _render_task(later, [], [])


# ---------------------------------------------------------------------------
# Events -> tasks (pure)
# ---------------------------------------------------------------------------
def test_event_task_gids_maps_each_event_shape():
    events = [
        comment_added("s1", "t-added"),
        comment_edited("s2", "t-edited"),  # parent is null: resolved through target
        task_changed("t-renamed"),
        # subtask added under a task: the parent is the document to refresh
        {"action": "added", "resource": _task_ref("sub1"), "parent": _task_ref("t-parent")},
        # a deletion names only the task; for a subtask the state map finds its parent
        {"action": "deleted", "resource": _task_ref("t-deleted"), "parent": None},
        # ignored: project-level stories and other resource types
        {
            "action": "added",
            "resource": {"gid": "s3", "resource_type": "story"},
            "parent": {"gid": "P1", "resource_type": "project"},
        },
        {"action": "changed", "resource": {"gid": "sec", "resource_type": "section"}},
    ]
    assert _event_task_gids(events) == {
        "t-added",
        "t-edited",
        "t-renamed",
        "t-parent",
        "t-deleted",
    }


def test_is_rename_only_for_project_or_section_name_changes():
    def changed(kind, field):
        return {
            "action": "changed",
            "resource": {"gid": "x", "resource_type": kind},
            "change": {"field": field},
        }

    assert _is_rename([changed("section", "name")]) is True
    assert _is_rename([changed("project", "name")]) is True
    assert _is_rename([changed("project", "notes")]) is False  # description edit
    assert _is_rename([changed("task", "name")]) is False  # a task rename is per-task
    assert (
        _is_rename([{"action": "added", "resource": {"gid": "x", "resource_type": "section"}}])
        is False
    )
    assert _is_rename([]) is False


def test_documents_for_maps_subtasks_to_their_parent_and_drops_the_rest():
    swept = {"1", "2"}
    subtask_parents = {"10": "1", "30": "3"}  # task 3 is no longer in the selection

    assert _documents_for({"1", "10", "30", "unknown"}, swept, subtask_parents) == {"1"}
    assert _documents_for(set(), swept, subtask_parents) == set()


# ---------------------------------------------------------------------------
# sync_tasks: first sync / incremental
# ---------------------------------------------------------------------------
def test_first_sync_renders_tasks_with_comments_and_subtasks(fake):
    fake.add_task(
        "1", modified_at=day(1), name="Design homepage", notes="Hero and footer.",
        assignee="Ada", comments=[("Grace", "Use the new palette")],
    )  # fmt: skip
    fake.add_task("2", modified_at=day(2), name="Write copy")
    fake.add_task("10", modified_at=day(1), name="Pick fonts", parent="1")
    state = {}

    rows = run_sync(fake, state)

    tasks = task_rows(rows)
    assert set(tasks) == {"task:1", "task:2"}  # the subtask is not its own document
    assert tasks["task:1"]["title"] == "Design homepage"
    assert tasks["task:1"]["url"] == "https://app.asana.com/t/1"
    content = tasks["task:1"]["content"]
    assert "Hero and footer." in content
    assert "Assignee: Ada" in content
    assert "Project: Website launch (section: To do)" in content
    assert "- [ ] Pick fonts" in content
    assert "- Grace: Use the new palette" in content
    assert "added this task to a project" not in content  # system stories are dropped
    assert "2026-10-" not in content  # no timestamps in the text

    project = next(r for r in rows if r["id"] == "project:P1")
    assert (project["title"], project["content"]) == ("Website launch", "Ship the new site.")

    assert tombstones(rows) == []
    assert state["known_ids"] == ["project:P1", "task:1", "task:2"]
    assert state["subtasks"] == {"10": "1"}  # remembered so subtask events find task 1
    assert state["projects"]["P1"]["cursor"] == day(2)
    assert state["projects"]["P1"]["sync"] == fake.tokens["P1"]  # token from the 412


def test_second_sync_without_changes_fetches_no_tasks(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.calls.clear()

    rows = run_sync(fake, state)

    assert [r["id"] for r in rows] == ["project:P1"]  # only the project description
    assert not any(path.startswith("/tasks/") for path, _ in fake.calls)  # nothing re-fetched
    # One listing per project, with every task's modified_at.
    listing = {"project": "P1", "opt_fields": "modified_at", "limit": 100}
    assert fake.requested("/tasks") == [listing]
    assert state["known_ids"] == before["known_ids"]
    assert state["projects"]["P1"]["cursor"] == day(2)


def test_edited_task_is_resynced_and_advances_cursor(fake):
    fake.add_task("1", modified_at=day(1), notes="v1")
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)

    fake.tasks["1"].update(notes="v2", modified_at=day(5))
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1"}
    assert "v2" in task_rows(rows)["task:1"]["content"]
    assert state["projects"]["P1"]["cursor"] == day(5)


def test_overlap_start_is_sixty_seconds_before_the_cursor_in_asana_format():
    assert _overlap_start("2026-10-09T10:28:11.128Z") == "2026-10-09T10:27:11.128Z"
    assert _overlap_start("2026-10-09T00:00:30.000Z") == "2026-10-08T23:59:30.000Z"
    assert _overlap_start("") == ""  # no cursor yet


def test_task_modified_in_the_same_millisecond_as_the_cursor_is_not_missed(fake):
    # Task 1 sets the cursor. Task 2 is then modified with the very same
    # timestamp, after the listing ran and with no event delivered. A plain
    # "newer than the cursor" rule would skip it for good.
    fake.add_task("1", modified_at=day(2), notes="sets the cursor")
    fake.add_task("2", modified_at=day(1), notes="v1")
    state = {}
    run_sync(fake, state)
    assert state["projects"]["P1"]["cursor"] == day(2)
    assert state["projects"]["P1"]["seen"] == {"1": day(2)}

    fake.tasks["2"].update(notes="v2", modified_at=day(2))
    fake.calls.clear()
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:2"}
    assert "v2" in task_rows(rows)["task:2"]["content"]
    assert not fake.requested("/tasks/1")  # the task already seen at the cursor is skipped
    assert state["projects"]["P1"]["seen"] == {"1": day(2), "2": day(2)}

    fake.calls.clear()
    assert task_rows(run_sync(fake, state)) == {}  # and it is not fetched again


def test_task_stamped_just_before_the_cursor_is_caught_by_the_overlap(fake):
    # A change stamped a few seconds before the cursor that the previous listing
    # did not include yet falls inside the overlap window and is rendered once.
    fake.add_task("1", modified_at="2026-10-02T00:00:00.000Z")
    fake.add_task("2", modified_at=day(1), notes="v1")
    state = {}
    run_sync(fake, state)

    fake.tasks["2"].update(notes="v2", modified_at="2026-10-01T23:59:40.000Z")
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:2"}
    assert state["projects"]["P1"]["cursor"] == "2026-10-02T00:00:00.000Z"  # never moves back
    assert task_rows(run_sync(fake, state)) == {}


def test_new_comment_is_picked_up_through_modified_since(fake):
    # Adding a comment bumps the task's modified_at (verified live), so the
    # cursor alone finds it; no event is needed.
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)

    fake.tasks["1"]["comments"].append(("Grace", "Any update?"))
    fake.tasks["1"]["modified_at"] = day(3)
    rows = run_sync(fake, state)

    assert "- Grace: Any update?" in task_rows(rows)["task:1"]["content"]


def test_edited_comment_is_picked_up_through_events(fake):
    # Editing a comment does NOT bump modified_at (verified live), so
    # modified_since returns nothing; the story event is the only signal, and it
    # carries parent=null.
    fake.add_task("1", modified_at=day(1), comments=[("Grace", "first draft")])
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)

    fake.tasks["1"]["comments"][0] = ("Grace", "final wording")
    fake.emit("P1", **comment_edited("s1", "1"))
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1"}
    assert "- Grace: final wording" in task_rows(rows)["task:1"]["content"]
    assert state["projects"]["P1"]["cursor"] == day(2)  # the cursor did not need to move


def test_renamed_subtask_refreshes_its_parent_through_events(fake):
    # A subtask rename bumps neither task and arrives as a task event for the
    # subtask gid with no parent (verified live); state knows which task holds it.
    fake.add_task("1", modified_at=day(1))
    fake.add_task("10", modified_at=day(1), name="Old name", parent="1")
    state = {}
    run_sync(fake, state)

    fake.tasks["10"]["name"] = "New name"
    fake.emit("P1", **task_changed("10"))
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1"}  # the parent, not a document for the subtask
    assert "- [ ] New name" in task_rows(rows)["task:1"]["content"]
    assert "task:10" not in state["known_ids"]


def test_changed_subtask_details_refresh_the_parent_through_events(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("10", modified_at=day(1), name="Screenshots", parent="1")
    state = {}
    assert task_rows(run_sync(fake, state))["task:1"]["content"].endswith("- [ ] Screenshots")

    fake.tasks["10"].update(assignee="Grace", due_on="2026-10-18", notes="Light theme only.")
    fake.emit("P1", **task_changed("10"))
    content = task_rows(run_sync(fake, state))["task:1"]["content"]

    assert content.endswith(
        "- [ ] Screenshots\n  Assignee: Grace\n  Due: 2026-10-18\n  Notes: Light theme only."
    )


@pytest.mark.parametrize("renamed", ["section", "project"])
def test_renamed_section_or_project_rerenders_the_whole_project(fake, renamed):
    # A rename touches no task (verified live: modified_at stays put), but every
    # task document names its project and section. The event is the only signal.
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)

    if renamed == "section":
        for task in fake.tasks.values():
            task["section"] = "Backlog"
        expected = "Project: Website launch (section: Backlog)"
    else:
        fake.projects["P1"]["name"] = "Site relaunch"
        expected = "Project: Site relaunch (section: To do)"
    fake.emit(
        "P1",
        action="changed",
        resource={"gid": "x", "resource_type": renamed},
        parent=None,
        change={"field": "name"},
    )
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1", "task:2"}  # no timestamp moved, all re-rendered
    assert all(expected in row["content"] for row in task_rows(rows).values())
    assert state["projects"]["P1"]["cursor"] == day(2)

    fake.calls.clear()
    assert task_rows(run_sync(fake, state)) == {}  # one-off: the next run is incremental again


def test_edited_project_description_does_not_rerender_tasks(fake):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)

    fake.projects["P1"]["notes"] = "New description."
    fake.emit(
        "P1",
        action="changed",
        resource={"gid": "P1", "resource_type": "project"},
        parent=None,
        change={"field": "notes"},
    )
    rows = run_sync(fake, state)

    assert task_rows(rows) == {}  # task documents do not contain the description
    assert next(r for r in rows if r["id"] == "project:P1")["content"] == "New description."


def test_deleted_subtask_refreshes_its_parent_through_events(fake):
    # Deleting a subtask does not bump the parent, and the "deleted" event has no
    # parent (verified live). The deleted subtask can no longer be fetched either,
    # so the subtask map in state is the only way back to the parent document.
    fake.add_task("1", modified_at=day(1))
    fake.add_task("10", modified_at=day(1), name="Obsolete step", parent="1")
    state = {}
    assert "Obsolete step" in task_rows(run_sync(fake, state))["task:1"]["content"]

    del fake.tasks["10"]
    fake.emit("P1", action="deleted", resource=_task_ref("10"), parent=None)
    rows = run_sync(fake, state)

    assert "Obsolete step" not in task_rows(rows)["task:1"]["content"]
    assert tombstones(rows) == []  # the subtask never was a document of its own
    assert state["subtasks"] == {}


def test_subtask_map_is_dropped_with_its_parent(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("10", modified_at=day(1), parent="1")
    state = {}
    run_sync(fake, state)

    del fake.tasks["1"], fake.tasks["10"]
    run_sync(fake, state)

    assert state["subtasks"] == {}


def test_events_about_tasks_outside_the_selection_are_ignored(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_project("P2")
    fake.add_task("99", modified_at=day(1), projects=("P2",))  # in a project we do not sync
    state = {}
    run_sync(fake, state)

    fake.emit("P1", **task_changed("99"))
    fake.emit("P1", **task_changed("unknown"))
    fake.calls.clear()
    rows = run_sync(fake, state)

    assert task_rows(rows) == {}
    assert tombstones(rows) == []
    assert not any(path.startswith("/tasks/") for path, _ in fake.calls)  # no lookups either


def test_task_in_two_projects_is_emitted_once(fake):
    fake.add_project("P2", name="Blog")
    fake.add_task("1", modified_at=day(1), projects=("P1", "P2"))
    state = {}

    rows = run_sync(fake, state, project_gids=("P1", "P2"))

    assert [r["id"] for r in rows if r["id"].startswith("task:")] == ["task:1"]
    assert len(fake.requested("/tasks/1")) == 1  # fetched once, not once per project
    assert state["known_ids"] == ["project:P1", "project:P2", "task:1"]


def test_task_new_to_the_project_with_an_old_timestamp_is_ingested(fake):
    fake.add_task("1", modified_at=day(5))
    state = {}
    run_sync(fake, state)

    # Appears in the sweep, but modified_since (cursor = day 5) does not return it.
    fake.add_task("2", modified_at=day(1), name="Moved in")
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:2"}


def test_include_completed_false_skips_and_forgets_completed_tasks(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1), completed=True)
    state = {}

    assert set(task_rows(run_sync(fake, state, include_completed=False))) == {"task:1"}
    assert all(p.get("completed_since") == "now" for p in fake.requested("/tasks"))

    fake.tasks["1"].update(completed=True, modified_at=day(2))
    rows = run_sync(fake, state, include_completed=False)
    assert tombstones(rows) == [{"id": "task:1", "_deleted": True}]


# ---------------------------------------------------------------------------
# sync_tasks: events token (412)
# ---------------------------------------------------------------------------
def test_stale_sync_token_stores_fresh_one_and_rerenders_everything(fake):
    # After a 412 the events in the gap are lost, and a comment edited in the gap
    # is invisible to modified_since. Re-rendering the project is what catches it.
    fake.add_task("1", modified_at=day(1), comments=[("Grace", "first draft")])
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)
    old_token = state["projects"]["P1"]["sync"]

    fake.tasks["1"]["comments"][0] = ("Grace", "edited during the gap")
    fake.emit("P1", **comment_edited("s1", "1"))  # dropped by the fake on 412, as in Asana
    fake.expire_token("P1")
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1", "task:2"}
    assert "edited during the gap" in task_rows(rows)["task:1"]["content"]
    assert state["projects"]["P1"]["sync"] not in (None, old_token)
    assert state["projects"]["P1"]["sync"] == fake.tokens["P1"]

    fresh_token = state["projects"]["P1"]["sync"]
    fake.calls.clear()
    assert task_rows(run_sync(fake, state)) == {}  # back to incremental on the next run
    assert fake.requested("/events")[0]["sync"] == fresh_token  # the stored token was sent


def test_events_response_without_sync_token_is_an_error(fake):
    fake.add_task("1", modified_at=day(1))
    fake.queue("/events", _Resp(412, {"errors": [{"message": "no token here"}]}))
    state = {}

    with pytest.raises(AsanaResponseError, match="no sync token"):
        run_sync(fake, state)
    assert state == {}


# ---------------------------------------------------------------------------
# sync_tasks: deletion
# ---------------------------------------------------------------------------
def test_deleted_task_emits_hard_delete_marker(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(2))
    state = {}
    run_sync(fake, state)

    del fake.tasks["2"]
    rows = run_sync(fake, state)

    assert tombstones(rows) == [{"id": "task:2", "_deleted": True}]
    assert state["known_ids"] == ["project:P1", "task:1"]


@pytest.mark.parametrize("status", [403, 404, 500])
def test_listed_task_whose_get_fails_is_kept_and_retried(fake, status):
    # The sweep lists task 1, but its own GET fails: that does not prove it is
    # gone. Keep the document, retry next run, and let the listing decide.
    fake.add_task("1", modified_at=day(1), notes="v1")
    fake.add_task("2", modified_at=day(1))
    state = {}
    run_sync(fake, state)

    fake.tasks["1"].update(notes="v2", modified_at=day(4))
    # Task 2 moves the cursor to day 9, so task 1 (day 4) falls outside the
    # next run's overlap window: only state["retry"] can bring it back.
    fake.tasks["2"]["modified_at"] = day(9)
    for _ in range(asana._MAX_RETRIES if status == 500 else 1):
        fake.queue("/tasks/1", _error(status, "nope"))
    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:2"}  # the rest of the run happened
    assert tombstones(rows) == []
    assert "task:1" in state["known_ids"]
    assert state["retry"] == ["1"]
    assert "1" not in state["projects"]["P1"]["seen"]

    # Asana recovered; task 1 is not modified again, the retry brings it back.
    rows = run_sync(fake, state)
    assert "v2" in task_rows(rows)["task:1"]["content"]
    assert state["retry"] == []


def test_retry_entry_of_a_task_that_left_the_listing_is_dropped(fake):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["1"]["modified_at"] = day(4)
    fake.queue("/tasks/1", _error(403, "Forbidden"))
    run_sync(fake, state)
    assert state["retry"] == ["1"]

    del fake.tasks["1"]  # now the listing says it is gone
    fake.calls.clear()
    rows = run_sync(fake, state)

    assert tombstones(rows) == [{"id": "task:1", "_deleted": True}]
    assert state["retry"] == []
    assert not fake.requested("/tasks/1")


def _task_failure(status, gid):
    return [(f"/tasks/{gid}", _error(status, "nope")) for _ in range(asana._MAX_RETRIES)]


@pytest.mark.parametrize("failure", ["401", "rate limit", "long retry-after", "outage"])
def test_systemic_task_failure_stops_cleanly_and_resumes(fake, failure, sleeps):
    for gid in ("1", "2", "3", "4", "5"):
        fake.add_task(gid, modified_at=day(1), notes="v1")
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2", "3", "4", "5"):
        fake.tasks[gid].update(notes="v2", modified_at=day(4))
    # Task 1 moves the cursor past the others' overlap window, so only pending
    # (or the retry list) can bring tasks 2-4 back on the next run.
    fake.tasks["1"]["modified_at"] = day(9)
    del fake.tasks["5"]  # a real deletion waits for a run that finishes

    if failure == "401":
        fake.queue("/tasks/3", _error(401, "Not Authorized"))
    elif failure == "rate limit":
        for path, response in _task_failure(429, "3"):
            fake.queue(path, response)
    elif failure == "long retry-after":
        fake.queue("/tasks/3", _error(429, "Rate limited", headers={"Retry-After": "3600"}))
    else:
        for gid in ("2", "3", "4"):
            for path, response in _task_failure(500, gid):
                fake.queue(path, response)
    stats = {}
    rows = run_sync(fake, state, stats=stats)

    reasons = {"401": "auth", "rate limit": "rate_limit", "long retry-after": "rate_limit"}
    reason = reasons.get(failure, "unavailable")
    assert stats[f"failed_{reason}"] == 1 and stats["failed"] >= 1
    assert all(wait <= asana._MAX_WAIT_SECONDS for wait in sleeps)
    assert tombstones(rows) == []  # nothing deleted on a stopped run
    if failure == "outage":
        assert set(task_rows(rows)) == {"task:1"}
        assert state["retry"] == ["2", "3", "4"] and state["pending"] == []
    else:
        assert set(task_rows(rows)) == {"task:1", "task:2"}
        assert state["pending"] == ["3", "4"] and state["retry"] == []
    assert "task:5" in state["known_ids"]

    # Next run, with no upstream change: the rest is rendered and task 5 forgotten.
    rows = run_sync(fake, state)
    rest = {"task:2", "task:3", "task:4"} if failure == "outage" else {"task:3", "task:4"}
    assert set(task_rows(rows)) == rest
    assert all("v2" in row["content"] for row in task_rows(rows).values())
    assert tombstones(rows) == [{"id": "task:5", "_deleted": True}]
    assert state["pending"] == [] and state["retry"] == []


def test_401_before_anything_was_rendered_raises(fake):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.tasks["1"]["modified_at"] = day(4)
    fake.queue("/tasks/1", _error(401, "Not Authorized"))

    with pytest.raises(AsanaAPIError) as excinfo:
        run_sync(fake, state)
    assert excinfo.value.status == 401
    assert state == before


def test_rate_limit_while_listing_stops_cleanly_with_nothing_moved(fake):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    del fake.tasks["1"]
    fake.queue("/tasks", _error(429, "Rate limited", headers={"Retry-After": "600"}))
    stats = {}

    assert run_sync(fake, state, stats=stats) == []
    assert stats["failed_rate_limit"] == 1
    assert state == before


def test_three_unreadable_tasks_never_block_the_run_and_are_given_up(fake):
    # Tasks 11, 12 and 13 always answer 404 while still listed, next to each other.
    for gid in ("10", "11", "12", "13", "14"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("10", "11", "12", "13", "14"):
        fake.tasks[gid]["modified_at"] = day(4)

    def forbid():
        for gid in ("11", "12", "13"):
            fake.queue(f"/tasks/{gid}", _error(404, "Not found"))

    for run in (1, 2):
        forbid()
        stats = {}
        rows = run_sync(fake, state, stats=stats, now=hours(12 * run))
        if run == 1:
            assert set(task_rows(rows)) == {"task:10", "task:14"}  # the others went on
        else:
            assert stats["probe_task"] == 1  # 10 and 14 are unchanged: one is read as a check
        assert tombstones(rows) == []
        assert state["retry"] == ["11", "12", "13"]
        runs = {gid: entry["runs"] for gid, entry in state["unreadable"].items()}
        assert runs == {"11": run, "12": run, "13": run}
        assert stats["kept"] == 3 and "failed_unavailable" not in stats
        assert "failed_permission" not in stats

    # A task created upstream meanwhile is still ingested.
    fake.add_task("15", modified_at=day(5))
    forbid()
    stats = {}
    rows = run_sync(fake, state, stats=stats, now=hours(36))
    assert "task:15" in task_rows(rows)
    # Third run, 24 hours after the first: forgotten, and no longer asked about.
    assert [r["id"] for r in tombstones(rows)] == ["task:11", "task:12", "task:13"]
    assert stats["given_up"] == 3 and state["retry"] == [] and state["unreadable"] == {}
    fake.calls.clear()
    run_sync(fake, state, now=hours(48))
    assert not fake.requested("/tasks/11")

    # Once a given-up task changes upstream, it is asked about (and ingested) again.
    fake.tasks["12"]["modified_at"] = day(6)
    rows = run_sync(fake, state, now=hours(48))
    assert set(task_rows(rows)) == {"task:12"}
    assert "12" not in state["given_up"]


def test_failing_runs_within_a_day_forget_nothing_and_a_recovery_clears_them(fake):
    # A host that syncs every ten minutes sees four runs of 404s in under an hour.
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)

    for minutes in (10, 20, 30, 40):
        fake.queue("/tasks/2", _error(404, "Not found"))
        rows = run_sync(fake, state, now=NOW + timedelta(minutes=minutes))
        assert tombstones(rows) == []
    assert state["unreadable"]["2"]["runs"] == 4
    assert state["given_up"] == {}

    # Asana recovers: the task is read again and its failures are forgotten.
    rows = run_sync(fake, state, now=hours(1))
    assert set(task_rows(rows)) == {"task:2"}
    assert state["unreadable"] == {} and state["retry"] == []


@pytest.mark.parametrize("check", [403, 500])
def test_a_run_that_cannot_count_never_gives_up_an_old_entry(fake, check, caplog):
    # Task 2 was counted three times within an hour. A day later the check fails
    # too: that run counts nothing, so it cannot forget task 2 either.
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)
    for minutes in (10, 20, 30):
        fake.queue("/tasks/2", _error(404, "Not found"))
        run_sync(fake, state, now=NOW + timedelta(minutes=minutes))
    assert state["unreadable"]["2"]["runs"] == 3

    fake.queue("/tasks/2", _error(403, "Forbidden"))
    for path, response in _task_failure(check, "1")[: 1 if check < 500 else None]:
        fake.queue(path, response)
    stats = {}
    rows = run_sync(fake, state, stats=stats, now=hours(25))

    assert tombstones(rows) == [] and stats["given_up"] == 0
    assert state["unreadable"]["2"]["runs"] == 3 and state["given_up"] == {}
    assert ("failed_permission" in stats) == (check == 403)


def test_a_partial_outage_answering_404_everywhere_forgets_nothing(fake, caplog):
    # Every GET /tasks/N answers 404, the check of an unchanged task included.
    for gid in ("1", "2", "3", "4"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2", "3"):
        fake.tasks[gid]["modified_at"] = day(4)

    for run in (1, 2, 3, 4):
        for gid in ("1", "2", "3", "4"):
            fake.queue(f"/tasks/{gid}", _error(404, "Not found"))
        stats = {}
        rows = run_sync(fake, state, stats=stats, now=hours(24 * run))
        assert tombstones(rows) == []
        assert stats["probe_task"] == 1 and stats["given_up"] == 0
        assert stats["failed_permission"] == 3
        assert state["unreadable"] == {}
    assert "check the token's permissions" in caplog.text
    assert {"task:1", "task:2", "task:3", "task:4"} <= set(state["known_ids"])


def test_a_lost_permission_never_gives_anything_up(fake, caplog):
    # Every task answers 403 and there is no other task to compare with: kept,
    # never forgotten, and the log says nothing counts.
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2", "3"):
        fake.tasks[gid]["modified_at"] = day(4)

    for run in (1, 2, 3, 4):
        for gid in ("1", "2", "3"):
            fake.queue(f"/tasks/{gid}", _error(403, "Forbidden"))
        stats = {}
        rows = run_sync(fake, state, stats=stats, now=hours(24 * run))
        assert tombstones(rows) == []
        assert stats["failed_permission"] == 3 and stats["given_up"] == 0
        assert "probe_task" not in stats
        assert state["unreadable"] == {}
    assert "there is no other task to check access with" in caplog.text
    assert {"task:1", "task:2", "task:3"} <= set(state["known_ids"])


def test_one_hidden_task_among_unchanged_ones_is_given_up(fake, caplog):
    # Steady state: task 1 is unchanged and skipped, so only task 2 is fetched,
    # and task 1 is read once per run to show that access works.
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)

    for run in (1, 2, 3):
        fake.queue("/tasks/2", _error(403, "Forbidden"))
        fake.calls.clear()
        stats = {}
        rows = run_sync(fake, state, stats=stats, now=hours(12 * run))
        assert fake.requested("/tasks/1") == [{"opt_fields": "gid"}]  # the check, after
        assert stats["probe_task"] == 1 and "failed_permission" not in stats
        assert task_rows(rows) == {}  # the check is not emitted
    assert [r["id"] for r in tombstones(rows)] == ["task:2"]
    assert "check the token's permissions" not in caplog.text


def test_a_check_answering_a_server_error_counts_nothing(fake, caplog):
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)
    fake.queue("/tasks/2", _error(403, "Forbidden"))
    for path, response in _task_failure(500, "1"):  # Asana is failing: proves nothing
        fake.queue(path, response)
    stats = {}

    rows = run_sync(fake, state, stats=stats, now=hours(12))

    assert tombstones(rows) == []
    assert stats["probe_task"] == 1 and "failed_permission" not in stats
    assert state["unreadable"] == {} and state["retry"] == ["2"]
    assert "check the token's permissions" not in caplog.text


def test_a_given_up_task_is_asked_again_after_a_day(fake):
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)
    for run in (1, 2, 3):
        fake.queue("/tasks/2", _error(404, "Not found"))
        run_sync(fake, state, now=hours(12 * run))
    assert state["given_up"] == {"2": {"stamp": day(4), "at": asana._stamp(hours(36))}}

    # Not asked within the day; asked a day later, and still unreadable.
    fake.calls.clear()
    run_sync(fake, state, now=hours(59))
    assert not fake.requested("/tasks/2")
    fake.queue("/tasks/2", _error(404, "Not found"))
    stats = {}
    rows = run_sync(fake, state, stats=stats, now=hours(60))
    assert fake.requested("/tasks/2") and task_rows(rows) == {} and tombstones(rows) == []
    assert stats["failed"] == 1 and stats["kept"] == 0
    assert state["given_up"]["2"]["at"] == asana._stamp(hours(60))
    assert state["retry"] == [] and state["unreadable"] == {} and state["pending"] == []

    # Readable again a day later, though never edited: it comes back, and stays.
    rows = run_sync(fake, state, now=hours(84))
    assert set(task_rows(rows)) == {"task:2"}
    assert state["given_up"] == {} and "task:2" in state["known_ids"]
    assert tombstones(run_sync(fake, state, now=hours(85))) == []


def test_given_up_tasks_are_asked_again_a_few_per_run_oldest_first(fake, monkeypatch):
    monkeypatch.setattr(asana, "_MAX_REASKS", 1)
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    # Tasks 2 and 3 were forgotten two and three days ago.
    state["given_up"] = {
        "2": {"stamp": day(1), "at": asana._stamp(hours(-48))},
        "3": {"stamp": day(1), "at": asana._stamp(hours(-72))},
    }
    state["known_ids"] = ["project:P1", "task:1"]
    fake.add_task("4", modified_at=day(5))
    fake.calls.clear()

    rows = run_sync(fake, state)

    fetched = [path for path, _ in fake.calls if path in ("/tasks/2", "/tasks/3", "/tasks/4")]
    assert fetched == ["/tasks/4", "/tasks/3"]  # new tasks first, then the oldest one
    assert set(task_rows(rows)) == {"task:3", "task:4"}
    assert list(state["given_up"]) == ["2"]


def test_a_run_that_stops_early_counts_nothing_toward_giving_up(fake):
    fake.add_project("P2")
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state, project_gids=("P1", "P2"))
    fake.tasks["2"]["modified_at"] = day(4)
    for run in (1, 2):
        fake.queue("/tasks/2", _error(403, "Forbidden"))
        run_sync(fake, state, project_gids=("P1", "P2"), now=hours(12 * run))
    unreadable = copy.deepcopy(state["unreadable"])
    assert unreadable["2"]["runs"] == 2
    # Task 3 was forgotten a day ago and is due to be asked again.
    state["given_up"] = {"3": {"stamp": day(1), "at": asana._stamp(hours(24))}}
    state["known_ids"].remove("task:3")

    # Run 3: task 1 is read, task 2 answers 403 again, task 3 is still hidden,
    # then the token is revoked on a project and the run stops.
    fake.tasks["1"]["modified_at"] = day(6)
    fake.queue("/tasks/2", _error(403, "Forbidden"))
    fake.queue("/tasks/3", _error(404, "Not found"))
    fake.queue("/projects/P2", _error(401, "Not Authorized"))
    stats = {}
    rows = run_sync(fake, state, project_gids=("P1", "P2"), stats=stats, now=hours(48))

    assert stats["failed_auth"] == 1 and tombstones(rows) == []
    assert state["unreadable"] == unreadable  # not a third run
    assert state["given_up"] == {"3": {"stamp": day(1), "at": asana._stamp(hours(24))}}


def test_state_from_the_second_round_is_read(fake):
    # Round-2 state: given_up held a bare stamp, unreadable had no ``since``.
    for gid in ("1", "2", "3", "4"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.tasks["2"]["modified_at"] = day(4)
    fake.tasks["4"]["modified_at"] = day(4)
    state["retry"] = ["2", "4"]
    state["unreadable"] = {"2": {"stamp": day(4), "runs": 2}, "4": {"stamp": day(4), "runs": 0}}
    state["given_up"] = {"3": day(1)}
    state["known_ids"].remove("task:3")
    fake.queue("/tasks/2", _error(404, "Not found"))
    fake.queue("/tasks/4", _error(404, "Not found"))

    rows = run_sync(fake, state, now=hours(1))

    # Task 3 has no time of its own: it sits at the epoch, so it is asked at once.
    assert asana._load_given_up({"3": day(1)}) == {
        "3": {"stamp": day(1), "at": asana._stamp(asana._EPOCH)}
    }
    assert "task:3" in task_rows(rows)
    assert state["given_up"] == {}
    # Task 2's day starts now; task 4's uncounted entry counts as its first run.
    assert state["unreadable"]["2"] == {"stamp": day(4), "runs": 3, "since": asana._stamp(hours(1))}
    assert state["unreadable"]["4"]["runs"] == 1
    assert tombstones(rows) == []


def test_retries_go_last_and_never_count_toward_an_outage(fake):
    for gid in ("1", "2", "3", "4", "5", "6", "7"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("2", "3", "4", "5", "6"):
        fake.tasks[gid]["modified_at"] = day(4)
    for gid in ("2", "4", "6"):  # kept after a 500, with successes between them
        for path, response in _task_failure(500, gid):
            fake.queue(path, response)
    run_sync(fake, state)
    assert state["retry"] == ["2", "4", "6"]

    # Still failing next run, three in a row at the end, after a fresh task.
    fake.tasks["7"]["modified_at"] = day(5)
    for gid in ("2", "4", "6"):
        for path, response in _task_failure(500, gid):
            fake.queue(path, response)
    fake.calls.clear()
    stats = {}
    rows = run_sync(fake, state, stats=stats)

    tasks = [path for path, _ in fake.calls if path.count("/") == 2 and path.startswith("/tasks/")]
    assert list(dict.fromkeys(tasks)) == ["/tasks/7", "/tasks/2", "/tasks/4", "/tasks/6"]
    assert set(task_rows(rows)) == {"task:7"}
    assert "failed_unavailable" not in stats and stats["failed"] == 3
    assert state["retry"] == ["2", "4", "6"] and state["pending"] == []


def test_a_success_between_server_errors_resets_the_outage_streak(fake):
    for gid in ("1", "2", "3", "4", "5"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2", "3", "4", "5"):
        fake.tasks[gid]["modified_at"] = day(4)
    for gid in ("1", "2", "4", "5"):  # 3 succeeds in between
        for path, response in _task_failure(500, gid):
            fake.queue(path, response)
    stats = {}

    rows = run_sync(fake, state, stats=stats)

    assert set(task_rows(rows)) == {"task:3"}
    assert "failed_unavailable" not in stats
    assert state["retry"] == ["1", "2", "4", "5"]


def test_task_body_of_the_wrong_shape_aborts(fake):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.tasks["2"]["modified_at"] = day(4)
    fake.queue("/tasks/2", _Resp(200, "<html>login</html>"))  # a login page with a 200

    with pytest.raises(AsanaResponseError):
        run_sync(fake, state)
    assert state == before


def test_two_failures_in_a_row_are_kept_and_a_success_resets_the_streak(fake):
    for gid in ("1", "2", "3", "4", "5"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2", "3", "4", "5"):
        fake.tasks[gid]["modified_at"] = day(4)
    for gid in ("1", "2", "4", "5"):  # 3 succeeds in between
        fake.queue(f"/tasks/{gid}", _error(404, "nope"))

    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:3"}
    assert tombstones(rows) == []
    assert state["retry"] == ["1", "2", "4", "5"]


@pytest.mark.parametrize("body", ["empty object", "data missing", "not JSON", "a list"])
def test_listing_with_a_body_of_the_wrong_shape_raises(fake, body):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    del fake.tasks["2"]

    response = {
        "empty object": _Resp(200, {}),
        "data missing": _Resp(200, {"next_page": None}),
        "not JSON": _Resp(200, None),
        "a list": _Resp(200, []),
    }[body]
    if body == "not JSON":

        def not_json():
            raise ValueError("Expecting value")

        response.json = not_json
    fake.queue("/tasks", response)

    with pytest.raises(AsanaResponseError):
        run_sync(fake, state)
    assert state == before  # never read as an empty listing, nothing deleted


def test_project_removed_from_selection_is_forgotten(fake):
    fake.add_project("P2", name="Blog")
    fake.add_task("1", modified_at=day(1), projects=("P1",))
    fake.add_task("2", modified_at=day(1), projects=("P2",))
    state = {}
    run_sync(fake, state, project_gids=("P1", "P2"))

    rows = run_sync(fake, state, project_gids=("P1",))

    assert [r["id"] for r in tombstones(rows)] == ["project:P2", "task:2"]
    assert set(state["projects"]) == {"P1"}


@pytest.mark.parametrize("failing_request", ["first sweep page", "second sweep page", "events"])
def test_failed_listing_deletes_nothing_and_keeps_state(fake, failing_request):
    # The single most important safety property: a listing that could not be
    # completed must never look like "these tasks were deleted".
    fake.page_size = 2
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    assert len(before["known_ids"]) == 4

    del fake.tasks["3"]  # a real deletion is pending too; it must wait for a clean run
    if failing_request == "events":
        fake.queue("/events", _error(403, "Forbidden"))
    else:
        on_second_page = failing_request == "second sweep page"
        fake.add_task("4", modified_at=day(1))  # keeps the sweep at two pages
        for _ in range(asana._MAX_RETRIES):  # outlast the retry budget
            fake.queue(
                "/tasks",
                _error(500, "Server Error"),
                when=lambda p: ("offset" in p) == on_second_page,
            )

    rows = []
    with pytest.raises(AsanaAPIError):
        for row in sync_tasks(fake, state, project_gids=["P1"]):
            rows.append(row)

    assert rows == []  # nothing emitted at all: no upserts, no hard-delete markers
    assert state == before  # ids, cursor and sync token untouched

    # The next clean run picks up exactly where the failed one would have.
    assert {"id": "task:3", "_deleted": True} in run_sync(fake, state)


# ---------------------------------------------------------------------------
# HTTP helpers: pagination, retries
# ---------------------------------------------------------------------------
def test_list_all_follows_the_returned_offset(fake):
    fake.page_size = 2
    for gid in ("1", "2", "3", "4", "5"):
        fake.add_task(gid, modified_at=day(1))

    items = _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})

    assert [item["gid"] for item in items] == ["1", "2", "3", "4", "5"]
    offsets = [params.get("offset") for params in fake.requested("/tasks")]
    assert offsets == [None, "offset:2", "offset:4"]  # only offsets Asana handed back


def test_expired_offset_restarts_the_listing(fake):
    fake.page_size = 2
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    fake.queue(
        "/tasks",
        _error(400, "offset: Your pagination token is invalid."),
        when=lambda p: "offset" in p,
    )

    items = _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})

    assert [item["gid"] for item in items] == ["1", "2", "3"]  # complete, no duplicates
    offsets = [params.get("offset") for params in fake.requested("/tasks")]
    assert offsets == [None, "offset:2", None, "offset:2"]  # restarted from the first page


def test_expired_offset_gives_up_after_the_restart_budget(fake):
    fake.page_size = 1
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    for _ in range(asana._MAX_LISTING_RESTARTS + 1):
        fake.queue("/tasks", _error(400, "offset: expired"), when=lambda p: "offset" in p)

    with pytest.raises(AsanaAPIError):
        _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})


def test_bad_request_on_the_first_page_is_not_retried(fake):
    fake.queue("/tasks", _error(400, "project: Not a recognized ID"))
    with pytest.raises(AsanaAPIError) as excinfo:
        _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})
    assert excinfo.value.detail == "project: Not a recognized ID"
    assert len(fake.requested("/tasks")) == 1


def test_rate_limit_waits_exactly_retry_after(fake, sleeps):
    fake.queue("/projects/P1", _error(429, "Rate limited", headers={"Retry-After": "7"}))

    status, body = _get(fake, "/projects/P1")

    assert status == 200 and body["data"]["name"] == "Website launch"
    assert sleeps == [7.0]


def test_server_and_network_errors_back_off_then_succeed(fake, sleeps):
    class Flaky(FakeAsana):
        failures = 1

        def get(self, url, params=None, timeout=None):
            if self.failures:
                self.failures -= 1
                raise requests.ConnectionError("connection reset")
            return super().get(url, params=params, timeout=timeout)

    session = Flaky()
    session.add_project("P1")
    session.queue("/projects/P1", _error(503, "Service Unavailable"))

    assert _get(session, "/projects/P1")[0] == 200
    assert sleeps == [1.0, 2.0]  # exponential backoff when there is no Retry-After


def test_retries_are_capped(fake, sleeps):
    for _ in range(asana._MAX_RETRIES):
        fake.queue("/projects/P1", _error(500, "Server Error"))

    with pytest.raises(AsanaAPIError, match="500"):
        _get(fake, "/projects/P1")
    assert len(sleeps) == asana._MAX_RETRIES - 1


def test_permanent_errors_are_not_retried(fake, sleeps):
    fake.queue("/projects/P1", _error(401, "Not Authorized"))
    with pytest.raises(AsanaAPIError) as excinfo:
        _get(fake, "/projects/P1")
    assert excinfo.value.status == 401
    assert str(excinfo.value) == "Asana /projects/P1: HTTP 401"
    assert excinfo.value.detail == "Not Authorized"
    assert sleeps == []


# ---------------------------------------------------------------------------
# asana_source: validation and dlt wiring
# ---------------------------------------------------------------------------
def test_asana_source_resource_is_configured_for_merge_and_hard_delete(fake):
    resource = asana_source(session=fake, project_gids=["P1"])
    assert resource.name == "asana_documents"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_asana_source_declares_document_marker_and_pipeline_scope(fake):
    source = asana_source(session=fake, project_gids=["P1"])
    # resolve_dlt_sources routes on this tag: rows become documents for cognify.
    assert ASANA_SOURCE_NAME == "asana"
    assert document_source_tag(source) == "asana"
    # A scoped pipeline name keeps cursors and tokens apart from other dlt sources.
    assert pipeline_name_for_source(source, "asana") != "ingest_dlt_source"


def test_resource_name_names_the_table_and_scopes_the_state(fake):
    default = asana_source(session=fake, project_gids=["P1"])
    work = asana_source(session=fake, project_gids=["P1"], resource_name="asana_work")

    assert work.name == "asana_work"
    assert pipeline_name_for_source(work, "asana") != pipeline_name_for_source(default, "asana")


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "task:1",
            "url": "https://app.asana.com/t/1",
            "title": "Design homepage",
            "content": "Completed: no",
        },
        content_hash="abc123",
        table_name="asana_documents",
    )
    data_id = uuid5(NAMESPACE_OID, "task:1")

    item = _build_document_data_item(row, data_id, "asana")

    assert item.system_metadata["source"] == "asana"
    assert item.system_metadata["url"] == "https://app.asana.com/t/1"
    assert item.system_metadata["external_id"] == "task:1"
    assert item.data == "# Design homepage\n\nCompleted: no"


def test_asana_source_requires_a_selection(fake):
    with pytest.raises(ValueError, match="project_gids"):
        asana_source(session=fake)


def test_asana_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ASANA_ACCESS_TOKEN"):
        asana_source(project_gids=["P1"])


def test_asana_source_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "test-token")
    assert asana_source(project_gids=["P1"]).name == "asana_documents"  # no request is made yet


# ---------------------------------------------------------------------------
# dlt pipeline: a real merge into a temp sqlite destination
# ---------------------------------------------------------------------------
def _pipeline(tmp_path, name):
    import dlt

    db_path = (tmp_path / "asana.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="asana_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _scope_name(**source_kwargs):
    """The dlt pipeline cognee would use for this source in dataset ``asana_ds``."""
    source = asana_source(session=FakeAsana(), **{"project_gids": ["P1"], **source_kwargs})
    return pipeline_name_for_source(source, "asana_ds")


def _run_pipeline(tmp_path, session, **source_kwargs):
    source = asana_source(session=session, **source_kwargs)
    # One dlt pipeline per source scope, the way cognee names them.
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "asana_ds"))
    pipeline.run(source)
    return pipeline


def _read_documents(pipeline, table="asana_documents"):
    """Return {id: content} for a documents table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def _chain(error):
    """An exception and everything it was raised from (dlt wraps source errors)."""
    chain = []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def test_pipeline_round_trip_edit_and_delete_across_two_runs(fake, tmp_path):
    fake.add_task("1", modified_at=day(1), notes="v1")
    fake.add_task("2", modified_at=day(2), notes="to be deleted")
    fake.add_task("3", modified_at=day(2), notes="untouched")

    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    assert set(documents) == {"task:1", "task:2", "task:3", "project:P1"}
    assert "v1" in documents["task:1"]

    # Between runs: task 1 edited, task 2 deleted, task 3 untouched.
    fake.tasks["1"].update(notes="v2", modified_at=day(6))
    del fake.tasks["2"]
    fake.calls.clear()

    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))

    # The cursor and id set survived in dlt resource state: only task 1 was fetched.
    assert fake.requested("/tasks/1") and not fake.requested("/tasks/3")
    assert set(documents) == {"task:1", "task:3", "project:P1"}  # task 2 hard-deleted
    assert "v2" in documents["task:1"] and "v1" not in documents["task:1"]
    assert "untouched" in documents["task:3"]


def test_pipeline_workspace_selection_syncs_every_project(fake, tmp_path):
    fake.add_project("P2", name="Blog")
    fake.add_task("1", modified_at=day(1), projects=("P1",))
    fake.add_task("2", modified_at=day(1), projects=("P2",))

    documents = _read_documents(_run_pipeline(tmp_path, fake, workspace_gid="W1"))

    assert set(documents) == {"task:1", "task:2", "project:P1", "project:P2"}
    assert fake.requested("/projects")[0]["workspace"] == "W1"


def test_pipeline_deletion_removes_the_row_from_the_destination(fake, tmp_path):
    # Goes red if the _deleted marker is not emitted or hard_delete is off:
    # the row must be gone, not kept emptied or with _deleted set.
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    _run_pipeline(tmp_path, fake, project_gids=["P1"])

    del fake.tasks["2"]
    pipeline = _run_pipeline(tmp_path, fake, project_gids=["P1"])

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT COUNT(*) FROM asana_documents WHERE id = 'task:2'") as cursor,
    ):
        assert cursor.fetchone()[0] == 0
    assert "task:1" in _read_documents(pipeline)


def test_pipeline_two_sources_in_one_dataset_stay_apart(fake, tmp_path):
    fake.add_project("P2", name="Blog")
    fake.add_task("1", modified_at=day(1), projects=("P1",))
    fake.add_task("2", modified_at=day(1), projects=("P2",))
    site = {"project_gids": ["P1"], "resource_name": "asana_site"}
    blog = {"project_gids": ["P2"], "resource_name": "asana_blog"}

    _run_pipeline(tmp_path, fake, **site)
    _run_pipeline(tmp_path, fake, **blog)
    site_docs = _read_documents(_run_pipeline(tmp_path, fake, **site), "asana_site")
    blog_docs = _read_documents(_run_pipeline(tmp_path, fake, **blog), "asana_blog")

    assert set(site_docs) == {"task:1", "project:P1"}
    assert set(blog_docs) == {"task:2", "project:P2"}


def test_check_active_is_asked_before_every_request(fake):
    fake.add_task("1", modified_at=day(1))
    checks = []
    source = asana_source(
        session=fake, project_gids=["P1"], check_active=lambda: checks.append(len(fake.calls))
    )
    assert list(source)
    # Asked before each request (and around each row), so never behind it.
    assert sorted(set(checks)) == list(range(len(fake.calls) + 1))


@pytest.mark.parametrize("stop_after_requests", [1, 4, 6])
def test_check_active_raising_stops_with_nothing_deleted_or_loaded(
    fake, tmp_path, stop_after_requests
):
    fake.add_task("1", modified_at=day(1), notes="v1")
    fake.add_task("2", modified_at=day(1))
    _run_pipeline(tmp_path, fake, project_gids=["P1"])
    before = _read_documents(_pipeline(tmp_path, _scope_name()))

    del fake.tasks["2"]
    fake.tasks["1"].update(notes="v2", modified_at=day(4))
    fake.calls.clear()

    class AccessRevokedError(Exception):
        pass

    def check_active():
        if len(fake.calls) >= stop_after_requests:
            raise AccessRevokedError

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, project_gids=["P1"], check_active=check_active)
    assert any(isinstance(error, AccessRevokedError) for error in _chain(excinfo.value))
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before

    # The state did not move: the next run does the deletion and the edit.
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    assert set(documents) == {"task:1", "project:P1"}
    assert "v2" in documents["task:1"]


def test_check_active_raising_an_oserror_is_not_retried_or_swallowed(fake, tmp_path, sleeps):
    # PermissionError is an OSError; only requests' own errors are retried.
    fake.add_task("1", modified_at=day(1))
    _run_pipeline(tmp_path, fake, project_gids=["P1"])
    before = _read_documents(_pipeline(tmp_path, _scope_name()))
    fake.tasks["1"].update(notes="v2", modified_at=day(4))

    def check_active():
        if fake.requested("/tasks/1"):  # the task's own GET has been made
            raise PermissionError("access revoked")

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, project_gids=["P1"], check_active=check_active)
    assert any(isinstance(error, PermissionError) for error in _chain(excinfo.value))
    assert sleeps == []  # not retried
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before


def test_pipeline_task_that_fails_on_its_own_is_kept_and_retried(fake, tmp_path):
    fake.add_task("1", modified_at=day(1), notes="v1")
    fake.add_task("2", modified_at=day(1), notes="v1")
    old = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))["task:2"]

    # Task 1 moves the cursor past task 2's overlap window, so only the retry
    # list can bring task 2 back on the next run.
    fake.tasks["1"].update(notes="v2", modified_at=day(9))
    fake.tasks["2"].update(notes="v2", modified_at=day(4))
    for _ in range(asana._MAX_RETRIES):  # a persistent 500 on task 2's stories
        fake.queue("/tasks/2/stories", _error(500, "Server Error"))
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))

    assert "v2" in documents["task:1"]  # the run completed
    assert documents["task:2"] == old  # kept as it was, not deleted

    fake.calls.clear()
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    assert fake.requested("/tasks/2") and not fake.requested("/tasks/1")
    assert "v2" in documents["task:2"]


def test_pipeline_401_mid_run_commits_progress_and_resumes(fake, tmp_path):
    # A token that expires on task 3 of 5: tasks 1-2 are kept, 3-5 come next run.
    for gid in ("1", "2", "3", "4", "5", "6"):
        fake.add_task(gid, modified_at=day(1), notes="v1")
    _run_pipeline(tmp_path, fake, project_gids=["P1"])
    for gid in ("1", "2", "3", "4", "5"):
        fake.tasks[gid].update(notes="v2", modified_at=day(4))
    fake.tasks["1"]["modified_at"] = day(9)  # past the others' overlap window
    del fake.tasks["6"]
    fake.queue("/tasks/3", _error(401, "Not Authorized"))

    source = asana_source(session=fake, project_gids=["P1"])
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "asana_ds"))
    pipeline.run(source)
    documents = _read_documents(pipeline)
    assert source.cognee_sync_stats["failed_auth"] == 1
    assert "v2" in documents["task:1"] and "v2" in documents["task:2"]
    assert "v1" in documents["task:3"]
    assert "task:6" in documents  # no deletion on a stopped run

    fake.calls.clear()  # no upstream change before the next run
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    fetched = {path for path, _ in fake.calls if path.count("/") == 2}
    assert {"/tasks/3", "/tasks/4", "/tasks/5"} <= fetched
    assert "/tasks/1" not in fetched
    assert all("v2" in documents[f"task:{gid}"] for gid in ("1", "2", "3", "4", "5"))
    assert "task:6" not in documents


def test_pipeline_failure_leaves_destination_and_state_untouched(fake, tmp_path):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    _run_pipeline(tmp_path, fake, project_gids=["P1"])

    del fake.tasks["2"]
    fake.queue("/tasks", _error(403, "Forbidden"))
    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, project_gids=["P1"])
    assert any(
        isinstance(error, AsanaAPIError) and error.status == 403 for error in _chain(excinfo.value)
    )

    # The failed run deleted nothing; the following clean run forgets task 2.
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    assert set(documents) == {"task:1", "project:P1"}


# ---------------------------------------------------------------------------
# Review round 2: selection, paging, events, rendering, secrets, wiring
# ---------------------------------------------------------------------------
def test_empty_selection_raises_instead_of_forgetting_everything(fake, tmp_path):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)

    with pytest.raises(ValueError, match="No Asana project selected"):
        run_sync(fake, state, project_gids=())
    assert state == before

    # A workspace whose project listing comes back empty does the same.
    _run_pipeline(tmp_path, fake, workspace_gid="W1")
    fake.projects.clear()
    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, workspace_gid="W1")
    assert any(isinstance(error, ValueError) for error in _chain(excinfo.value))
    fake.add_project("P1")
    documents = _read_documents(_run_pipeline(tmp_path, fake, workspace_gid="W1"))
    assert "task:1" in documents  # nothing was forgotten


def test_repeated_offset_stops_the_listing(fake):
    fake.queue("/tasks", _Resp(200, {"data": [], "next_page": {"offset": "SAME"}}))
    fake.queue("/tasks", _Resp(200, {"data": [], "next_page": {"offset": "SAME"}}))

    with pytest.raises(AsanaResponseError, match="came back twice"):
        _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})
    assert len(fake.requested("/tasks")) == 2


def test_listing_stops_after_the_page_cap(fake, monkeypatch):
    monkeypatch.setattr(asana, "_MAX_LIST_PAGES", 3)
    fake.page_size = 1
    for gid in ("1", "2", "3", "4", "5"):
        fake.add_task(gid, modified_at=day(1))

    with pytest.raises(AsanaResponseError, match="more than 3 pages"):
        _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})


def test_events_are_followed_across_has_more_pages(fake):
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    fake.events_page_size = 1
    for gid in ("1", "2", "3"):
        fake.emit("P1", **comment_edited(f"s{gid}", gid))  # invisible to modified_at

    rows = run_sync(fake, state)

    assert set(task_rows(rows)) == {"task:1", "task:2", "task:3"}
    assert len(fake.requested("/events")) == 1 + 3  # first run (a 412), then three pages


def test_events_without_a_gid_are_skipped():
    events = [
        {"action": "changed", "resource": {"resource_type": "task"}},
        {
            "action": "added",
            "resource": {"resource_type": "story"},
            "parent": {"resource_type": "task"},
        },
        {"action": "changed", "resource": {"resource_type": "story", "target": None}},
        task_changed("t1"),
    ]
    assert _event_task_gids(events) == {"t1"}


def test_only_selected_projects_are_named_in_a_task(fake):
    fake.add_project("P2", name="Other team")
    fake.add_task("1", modified_at=day(1), projects=("P1", "P2"))

    content = task_rows(run_sync(fake, {}, project_gids=("P1",)))["task:1"]["content"]

    assert "Project: Website launch" in content
    assert "Other team" not in content


class _NoScan(dict):
    """A subtask map that fails if anything walks all of it."""

    def items(self):
        raise AssertionError("full scan of the subtask map")

    def __iter__(self):
        raise AssertionError("full scan of the subtask map")


def test_subtask_map_is_updated_without_scanning_it():
    # 5,000 tasks with two subtasks each: replacing one task's entries must not
    # walk the whole map, or a large first sync turns quadratic.
    state = {"subtasks": {f"s{n}-{k}": f"t{n}" for n in range(5000) for k in (1, 2)}}
    run = asana._Run(asana._Client(None), state, {})
    run.subtask_parents = _NoScan(run.subtask_parents)

    for n in range(5000):
        run._remember_subtasks(f"t{n}", [{"gid": f"s{n}-1"}, {"gid": f"s{n}-new"}])

    assert dict.__len__(run.subtask_parents) == 10000
    assert dict.get(run.subtask_parents, "s7-new") == "t7"
    assert dict.get(run.subtask_parents, "s7-2") is None


SECRET = "SECRET-TOKEN-123"


def test_token_with_a_trailing_newline_is_refused_without_echoing_it():
    with pytest.raises(ValueError) as excinfo:
        asana_source(token=SECRET + "\n", project_gids=["P1"])
    assert SECRET not in str(excinfo.value)
    assert "whitespace" in str(excinfo.value)


def test_the_token_is_never_in_an_error_message_or_a_log_line(fake, caplog, sleeps):
    fake.add_task("1", modified_at=day(1))

    class Leaky(FakeAsana):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/projects/P1"):
                raise requests.exceptions.InvalidHeader(f"Invalid header value b'Bearer {SECRET}'")
            return super().get(url, params=params, timeout=timeout)

    leaky = Leaky()
    leaky.add_project("P1")
    with pytest.raises(AsanaAPIError) as excinfo:
        _get(leaky, "/projects/P1")
    assert SECRET not in str(excinfo.value) and excinfo.value.status == 0

    # An error whose message echoes the token: cut out of ``detail``.
    fake.headers = {"Authorization": f"Bearer {SECRET}"}
    fake.queue("/tasks", _error(400, f"bad token abcd-{SECRET}"))
    with pytest.raises(AsanaAPIError) as excinfo:
        run_sync(fake, {})
    assert excinfo.value.detail == "bad token abcd-[token]"

    # A 401 whose body echoes the token: neither the message nor the logs carry it.
    fake.queue("/tasks", _Resp(401, f"<html>token={SECRET}</html>"))
    with pytest.raises(AsanaAPIError) as excinfo:
        run_sync(fake, {})
    assert str(excinfo.value) == "Asana /tasks: HTTP 401"
    assert SECRET not in str(excinfo.value) and SECRET not in excinfo.value.detail
    assert SECRET not in caplog.text
    assert "InvalidHeader" in caplog.text  # the retries were logged by type


def test_waits_are_capped_and_ask_check_active_first(fake, sleeps):
    checks = []
    client = asana._Client(fake, lambda: checks.append(len(sleeps)))
    fake.queue("/projects/P1", _error(429, "Rate limited", headers={"Retry-After": "30"}))
    fake.queue("/projects/P1", _error(429, "Rate limited", headers={"Retry-After": "61"}))

    with pytest.raises(AsanaAPIError) as excinfo:
        _get(client, "/projects/P1")
    assert excinfo.value.status == 429
    assert sleeps == [30.0]  # the 61 s wait was not taken
    assert checks == [0, 0, 1]  # before the request, before the wait, before the retry


def test_redact_cuts_the_token_out_and_drops_text_for_a_short_one():
    assert asana._redact(f"bad token {SECRET}", SECRET) == "bad token [token]"
    assert asana._redact("task: Unknown object", SECRET) == "task: Unknown object"
    assert asana._redact("anything", "") == "anything"
    assert asana._redact("bad token abc", "abc") == ""  # too short to replace safely


def test_a_run_waits_at_most_its_budget_in_all(fake, sleeps):
    for gid in ("1", "2"):
        fake.add_task(gid, modified_at=day(1))
    state = {}
    run_sync(fake, state)
    for gid in ("1", "2"):
        fake.tasks[gid]["modified_at"] = day(4)
    for _ in range(asana._MAX_RETRIES):
        fake.queue("/tasks/1", _error(503, "Unavailable", headers={"Retry-After": "50"}))
    stats = {}

    rows = run_sync(fake, state, stats=stats)

    assert sleeps == [50.0, 50.0]  # a third wait would pass the 120 s budget
    assert stats["failed_unavailable"] == 1 and stats["failed"] == 1
    assert task_rows(rows) == {} and tombstones(rows) == []
    fake._queued.clear()
    assert set(task_rows(run_sync(fake, state))) == {"task:1", "task:2"}  # resumed


@pytest.mark.parametrize("name", ["", "Foo-Bar", "a" * 65, None])
def test_resource_name_is_validated(fake, name):
    with pytest.raises(ValueError, match="resource_name"):
        asana_source(service=fake, project_gids=["P1"], resource_name=name)


def test_service_is_the_injection_kwarg_and_session_its_alias(fake, tmp_path):
    fake.add_task("1", modified_at=day(1))
    assert list(asana_source(service=fake, project_gids=["P1"]))
    assert list(asana_source(session=fake, project_gids=["P1"]))
    with pytest.raises(ValueError, match="not both"):
        asana_source(service=fake, session=fake, project_gids=["P1"])


def test_session_is_built_per_run_and_closed(fake, monkeypatch):
    fake.add_task("1", modified_at=day(1))
    built = []

    class Closing(FakeAsana):
        closed = False

        def close(self):
            self.closed = True

    def make_session(token):
        session = Closing()
        session.__dict__.update(copy.deepcopy(fake.__dict__))
        built.append(session)
        return session

    monkeypatch.setattr(asana, "_make_session", make_session)
    source = asana_source(token="test-token", project_gids=["P1"])
    assert built == []  # nothing is opened before the run
    assert list(source)
    assert len(built) == 1 and built[0].closed


def test_sync_stats_count_what_a_run_did(fake):
    for gid in ("1", "2", "3"):
        fake.add_task(gid, modified_at=day(1))
    source = asana_source(service=fake, project_gids=["P1"])
    list(source)
    assert source.cognee_sync_stats == {
        "scanned": 3,
        "skipped": 0,
        "failed": 0,
        "deleted": 0,
        "kept": 0,
        "given_up": 0,
    }

    # A run stopped by a rate limit while listing says why, with no failure invented.
    fake.queue("/events", _error(429, "Rate limited", headers={"Retry-After": "120"}))
    list(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
