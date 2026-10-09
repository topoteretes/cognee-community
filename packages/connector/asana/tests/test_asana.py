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
  across two runs), plus the document-source tag core routes on.
"""

import copy
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_asana import asana
from cognee_community_connector_asana.asana import (
    ASANA_SOURCE_NAME,
    AsanaAPIError,
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
        delivered, self.events[project_gid] = self.events[project_gid], []
        self.tokens[project_gid] = fresh
        return _Resp(200, {"data": delivered, "sync": fresh, "has_more": False})

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


def run_sync(session, state, project_gids=("P1",), **kwargs):
    return list(sync_tasks(session, state, project_gids=list(project_gids), **kwargs))


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
    # The listing asked only for what changed since just before the cursor (day 2).
    assert fake.requested("/tasks")[1]["modified_since"] == "2026-10-01T23:59:00.000Z"
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

    with pytest.raises(AsanaAPIError, match="no sync token"):
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


def test_task_deleted_between_sweep_and_fetch_is_forgotten(fake):
    fake.add_task("1", modified_at=day(1))
    state = {}
    run_sync(fake, state)

    # Listed as changed, but gone by the time its detail is fetched.
    fake.tasks["1"]["modified_at"] = day(4)
    fake.queue("/tasks/1", _error(404, "task: Unknown object"))
    rows = run_sync(fake, state)

    assert task_rows(rows) == {}
    assert tombstones(rows) == [{"id": "task:1", "_deleted": True}]
    assert state["known_ids"] == ["project:P1"]


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
                when=lambda p: p["opt_fields"] == "gid" and ("offset" in p) == on_second_page,
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
    with pytest.raises(AsanaAPIError, match="Not a recognized ID"):
        _list_all(fake, "/tasks", {"project": "P1", "opt_fields": "gid"})
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
                raise ConnectionError("connection reset")  # an OSError, like requests'
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
    with pytest.raises(AsanaAPIError, match="Not Authorized") as excinfo:
        _get(fake, "/projects/P1")
    assert excinfo.value.status == 401
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
def _run_pipeline(tmp_path, session, **source_kwargs):
    import dlt

    db_path = (tmp_path / "asana.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="asana_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="asana_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(asana_source(session=session, **source_kwargs))
    return pipeline


def _read_documents(pipeline):
    """Return {id: content} for the asana_documents table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM asana_documents") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


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


def test_pipeline_failure_leaves_destination_and_state_untouched(fake, tmp_path):
    fake.add_task("1", modified_at=day(1))
    fake.add_task("2", modified_at=day(1))
    _run_pipeline(tmp_path, fake, project_gids=["P1"])

    del fake.tasks["2"]
    fake.queue("/tasks", _error(403, "Forbidden"))
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_pipeline(tmp_path, fake, project_gids=["P1"])

    # The failed run deleted nothing; the following clean run forgets task 2.
    documents = _read_documents(_run_pipeline(tmp_path, fake, project_gids=["P1"]))
    assert set(documents) == {"task:1", "project:P1"}
