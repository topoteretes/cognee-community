"""Unit tests for the Shortcut connector.

The Shortcut REST API is mocked by ``FakeShortcut``, an in-memory stand-in for
a ``requests`` session, so no token and no network are needed. Its behaviour
mirrors what was observed against a real workspace: ``POST /stories/search``
answers 201, has no pagination, includes both ends of a date window (compared
below the one-second resolution it displays) and returns nothing for an empty
filter; a deleted
comment stays in the story with ``deleted: true`` and no text; renaming or
deleting an epic, iteration or label leaves the stories' ``updated_at`` alone;
and every response carries a ``Date`` header.

Three layers:

* pure tests for rendering, the header fingerprint and the date-window listing;
* ``sync_stories`` driven with a plain dict as state (ingest, cursor, renames,
  deletion, and the failure paths that must not delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination (edit and delete
  across two runs), plus the document-source tag core routes on.
"""

import copy
import itertools
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_shortcut import shortcut
from cognee_community_connector_shortcut.shortcut import (
    SHORTCUT_SOURCE_NAME,
    ShortcutAPIError,
    ShortcutListingError,
    _fingerprint,
    _header_lines,
    _is_settled,
    _parse,
    _query_stories,
    _render_story,
    _request,
    _scope_filters,
    _server_time,
    _split_point,
    _stamp,
    shortcut_source,
    sync_stories,
)

API = "https://api.app.shortcut.com/api/v3"
SEARCH = "/stories/search"

BACKLOG, IN_PROGRESS, DONE = 500, 501, 502
ADA, GRACE = "member-ada", "member-grace"
TEAM = "team-web"


def at(day, second=0):
    """A deterministic Shortcut-style timestamp, ``at(1) < at(1, 5) < at(2)``."""
    return f"2024-03-{day:02d}T10:00:{second:02d}Z"


# "Now" for sync_stories, and what the fake's Date header says by default:
# well after every timestamp the tests use.
NOW = _parse("2024-03-28T00:00:00Z")
FIRST, LAST = _parse("2024-03-01T00:00:00Z"), _parse("2024-03-28T00:00:00Z")


# ---------------------------------------------------------------------------
# Fake Shortcut REST API
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
    return _Resp(status, {"message": message}, headers)


class FakeShortcut:
    """Minimal stand-in for a ``requests`` session talking to Shortcut."""

    _FILTERS = frozenset(
        {
            "created_at_start",
            "created_at_end",
            "updated_at_start",
            "updated_at_end",
            "archived",
            "group_ids",
            "epic_ids",
        }
    )

    def __init__(self):
        self.stories = {}  # id -> story dict (see add_story)
        self.epics = {}  # id -> epic dict
        self.iterations = {}  # id -> iteration dict
        self.labels = {}  # id -> name
        self.states = {BACKLOG: "Backlog", IN_PROGRESS: "In Progress", DONE: "Done"}
        self.members = {ADA: "Ada Lovelace", GRACE: "Grace Hopper"}
        self.clock = NOW  # what the Date header says; None sends no header
        self.hard_cap = None  # a server that silently truncates listings to this many
        self.calls = []  # (method, path, params or body) of every request
        self._queued = []  # (path, predicate, response) answered once, in order

    # -- fixture builders ----------------------------------------------------
    def add_story(self, story_id, *, updated_at, created_at=None, **fields):
        self.stories[story_id] = {
            "name": f"Story {story_id}",
            "description": "",
            "story_type": "feature",
            "workflow_state_id": BACKLOG,
            "archived": False,
            "owner_ids": [],
            "epic_id": None,
            "iteration_id": None,
            "group_id": None,
            "label_ids": [],
            "deadline": None,
            "comments": [],  # [{"id", "author_id", "text", "deleted"}]
            **fields,
            "created_at": created_at or updated_at,
            "updated_at": updated_at,
        }

    def add_epic(self, epic_id, name="Website launch", **fields):
        self.epics[epic_id] = {
            "name": name,
            "description": "Ship the new site.",
            "state": "in progress",
            "archived": False,
            "deadline": None,
            "group_ids": [],
            **fields,
        }

    def add_iteration(self, iteration_id, name="Sprint 12", **fields):
        self.iterations[iteration_id] = {
            "name": name,
            "description": "Two weeks of launch work.",
            "status": "started",
            "start_date": "2024-03-04",
            "end_date": "2024-03-15",
            "updated_at": at(1),
            "group_ids": [],
            **fields,
        }

    def delete_epic(self, epic_id):
        """As observed live: the stories lose the reference, their updated_at stays."""
        del self.epics[epic_id]
        for story in self.stories.values():
            if story["epic_id"] == epic_id:
                story["epic_id"] = None

    def delete_label(self, label_id):
        del self.labels[label_id]
        for story in self.stories.values():
            story["label_ids"] = [i for i in story["label_ids"] if i != label_id]

    def queue(self, path, response, when=lambda payload: True):
        """Answer the next matching request with ``response`` instead of real data."""
        self._queued.append((path, when, response))

    def requested(self, path):
        return [payload for _, called, payload in self.calls if called == path]

    def fetched_stories(self):
        """Ids of the stories fetched in full so far, in request order."""
        return [
            int(path.rsplit("/", 1)[1])
            for method, path, _ in self.calls
            if method == "GET" and path.startswith("/stories/")
        ]

    # -- requests.Session surface ----------------------------------------------
    def request(self, method, url, params=None, json=None, timeout=None):
        assert url.startswith(API + "/"), f"unexpected URL: {url}"
        assert timeout, "every request must carry a timeout"
        path = url[len(API) :]
        payload = dict(json or params or {})
        self.calls.append((method, path, payload))
        for index, (queued_path, when, response) in enumerate(self._queued):
            if queued_path == path and when(payload):
                del self._queued[index]
                return response
        response = self._route(method, path, payload)
        if self.clock is not None:
            date = self.clock if isinstance(self.clock, str) else format_datetime(self.clock, True)
            response.headers = {**response.headers, "Date": date}
        return response

    def _route(self, method, path, payload):
        parts = path.strip("/").split("/")
        if method == "POST":
            assert path == SEARCH, f"the connector must not write: POST {path}"
            return self._search(payload)
        assert method == "GET", f"the connector must only read: {method} {path}"
        if parts == ["workflows"]:
            states = [{"id": i, "name": name} for i, name in self.states.items()]
            return _Resp(200, [{"id": 1, "name": "Standard", "states": states}])
        if parts == ["members"]:
            return _Resp(200, [{"id": i, "profile": {"name": n}} for i, n in self.members.items()])
        if parts == ["epics"]:
            assert payload.get("includes_description") == "true", "epic text needs descriptions"
            return _Resp(200, [self._epic(i) for i in self.epics])
        if parts == ["iterations"]:
            # The list is slim: no description, as observed live.
            slim = [dict(self._iteration(i), description=None) for i in self.iterations]
            return _Resp(200, [{k: v for k, v in s.items() if k != "description"} for s in slim])
        if parts[0] == "iterations" and len(parts) == 2:
            if int(parts[1]) not in self.iterations:
                return _error(404, "Resource not found.")
            return _Resp(200, self._iteration(int(parts[1])))
        if parts[0] == "stories" and len(parts) == 2:
            if int(parts[1]) not in self.stories:
                return _error(404, "Resource not found.")
            return _Resp(200, self._story(int(parts[1]), full=True))
        raise AssertionError(f"unexpected path: {path}")

    # -- endpoints -------------------------------------------------------------
    def _search(self, filters):
        unknown = set(filters) - self._FILTERS
        if unknown:
            return _Resp(
                400, {"message": "invalid", "errors": dict.fromkeys(unknown, "disallowed-key")}
            )
        if not filters:
            return _Resp(201, [])  # as observed live: no filter, no stories
        matches = []
        for story_id, story in self.stories.items():
            if not self._in_window(story, filters, "created_at"):
                continue
            if not self._in_window(story, filters, "updated_at"):
                continue
            if "archived" in filters and story["archived"] != filters["archived"]:
                continue
            if "group_ids" in filters and story["group_id"] not in filters["group_ids"]:
                continue
            if "epic_ids" in filters and story["epic_id"] not in filters["epic_ids"]:
                continue
            matches.append(self._story(story_id, full=False))
        return _Resp(201, matches[: self.hard_cap])

    @staticmethod
    def _in_window(story, filters, field):
        # As observed live: timestamps are shown in whole seconds but compared
        # with their hidden fraction, and both ends of the window are included.
        # So ``end`` leaves out the rest of its own second, except for a story
        # stamped exactly on it (``fraction="000"``).
        start, end = filters.get(f"{field}_start"), filters.get(f"{field}_end")
        exact = story[field][:-1] + "." + story.get("fraction", "500")
        return (start is None or exact >= start[:-1] + ".000") and (
            end is None or exact <= end[:-1] + ".000"
        )

    def _story(self, story_id, full):
        story = self.stories[story_id]
        body = {
            "id": story_id,
            "app_url": f"https://app.shortcut.com/acme/story/{story_id}",
            "labels": [{"id": i, "name": self.labels[i]} for i in story["label_ids"]],
            **{k: v for k, v in story.items() if k not in ("description", "comments", "fraction")},
        }
        if full:
            body["description"] = story["description"]
            body["comments"] = [
                {**comment, "text": None if comment.get("deleted") else comment["text"]}
                for comment in story["comments"]
            ]
        return body

    def _epic(self, epic_id):
        return {
            "id": epic_id,
            "app_url": f"https://app.shortcut.com/acme/epic/{epic_id}",
            **self.epics[epic_id],
        }

    def _iteration(self, iteration_id):
        return {
            "id": iteration_id,
            "app_url": f"https://app.shortcut.com/acme/iteration/{iteration_id}",
            **self.iterations[iteration_id],
        }


def comment(comment_id, author, text, deleted=False):
    return {"id": comment_id, "author_id": author, "text": text, "deleted": deleted}


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry waits instead of sleeping."""
    recorded = []
    monkeypatch.setattr(shortcut.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def fake():
    return FakeShortcut()


def run_sync(session, state, **kwargs):
    return list(sync_stories(session, state, now=NOW, **kwargs))


def story_rows(rows):
    return {r["id"]: r for r in rows if r["id"].startswith("story:") and not r["_deleted"]}


def tombstones(rows):
    return [r for r in rows if r["_deleted"]]


NAMES = {
    "state": {BACKLOG: "Backlog", DONE: "Done"},
    "member": {ADA: "Ada Lovelace", GRACE: "Grace Hopper"},
    "epic": {7: "Website launch"},
    "iteration": {3: "Sprint 12"},
}

FULL_STORY = {
    "id": 1,
    "name": "Write launch post",
    "story_type": "feature",
    "workflow_state_id": DONE,
    "archived": False,
    "owner_ids": [GRACE, ADA],
    "epic_id": 7,
    "iteration_id": 3,
    "labels": [{"id": 2, "name": "marketing"}, {"id": 1, "name": "blog"}],
    "deadline": "2024-03-20T00:00:00Z",
    "description": "Draft the post, then review with marketing.\n",
    "comments": [
        comment(11, GRACE, "Use the new palette."),
        comment(12, ADA, "Done, see the draft.  "),
    ],
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_story_includes_header_description_and_comments():
    assert _render_story(FULL_STORY, NAMES) == (
        "Type: feature\n"
        "State: Done\n"
        "Owners: Ada Lovelace, Grace Hopper\n"
        "Epic: Website launch\n"
        "Iteration: Sprint 12\n"
        "Labels: blog, marketing\n"
        "Deadline: 2024-03-20\n"
        "\n"
        "Draft the post, then review with marketing.\n"
        "\n"
        "Comments:\n"
        "- Grace Hopper: Use the new palette.\n"
        "- Ada Lovelace: Done, see the draft."
    )


def test_render_story_minimal_and_archived():
    story = {"id": 1, "story_type": "bug", "workflow_state_id": BACKLOG, "archived": True}
    assert _render_story(story, NAMES) == "Type: bug\nState: Backlog\nArchived: yes"


def test_render_story_omits_deleted_comments():
    story = {
        **FULL_STORY,
        "comments": [
            {"id": 11, "author_id": GRACE, "text": None, "deleted": True},
            comment(12, ADA, "Still here."),
        ],
    }
    text = _render_story(story, NAMES)
    assert text.endswith("Comments:\n- Ada Lovelace: Still here.")

    story["comments"][1] = {"id": 12, "author_id": ADA, "text": None, "deleted": True}
    assert "Comments" not in _render_story(story, NAMES)


def test_render_story_ignores_volatile_fields():
    noisy = {
        **FULL_STORY,
        "updated_at": at(9),
        "position": 123456,
        "stats": {"num_related_documents": 4},
        "moved_at": at(8),
    }
    assert _render_story(noisy, NAMES) == _render_story(FULL_STORY, NAMES)


def test_names_that_cannot_be_resolved_are_left_out():
    # An epic, iteration, state or owner deleted a moment ago: absent, not an
    # error and not a raw id. A comment still needs an author to make sense.
    story = {
        **FULL_STORY,
        "workflow_state_id": 999,
        "owner_ids": ["member-gone", ADA],
        "epic_id": 999,
        "iteration_id": 999,
        "comments": [comment(11, "member-gone", "Old note.")],
    }
    text = _render_story(story, NAMES)
    assert _header_lines(story, NAMES) == [
        "Type: feature",
        "Owners: Ada Lovelace",
        "Labels: blog, marketing",
        "Deadline: 2024-03-20",
    ]
    assert "999" not in text and "member-gone" not in text
    assert "- Unknown: Old note." in text


# ---------------------------------------------------------------------------
# Header fingerprint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "change",
    [
        {"story_type": "bug"},
        {"workflow_state_id": BACKLOG},
        {"archived": True},
        {"owner_ids": [ADA]},
        {"epic_id": None},
        {"iteration_id": None},
        {"labels": [{"id": 1, "name": "blog"}]},
        {"labels": [{"id": 1, "name": "news"}, {"id": 2, "name": "marketing"}]},
        {"deadline": "2024-04-01T00:00:00Z"},
    ],
)
def test_fingerprint_changes_when_the_rendered_header_changes(change):
    before, after = FULL_STORY, {**FULL_STORY, **change}
    assert _header_lines(before, NAMES) != _header_lines(after, NAMES)
    assert _fingerprint(_header_lines(before, NAMES)) != _fingerprint(_header_lines(after, NAMES))
    # The header is literally the top of the document, so they cannot drift.
    assert _render_story(after, NAMES).startswith("\n".join(_header_lines(after, NAMES)))


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Another title"},
        {"description": "Rewritten."},
        {"comments": []},
        {"updated_at": at(9)},
        {"position": 5},
        {"owner_ids": [ADA, GRACE]},  # same owners, other order
        {"labels": [{"id": 1, "name": "blog"}, {"id": 2, "name": "marketing"}]},  # other order
        {"label_ids": [1, 2], "follower_ids": [ADA]},
    ],
)
def test_fingerprint_ignores_everything_outside_the_header(change):
    after = {**FULL_STORY, **change}
    assert _fingerprint(_header_lines(after, NAMES)) == _fingerprint(
        _header_lines(FULL_STORY, NAMES)
    )


def test_fingerprint_follows_a_renamed_lookup():
    renamed = {**NAMES, "epic": {7: "Site relaunch"}}
    assert _fingerprint(_header_lines(FULL_STORY, renamed)) != _fingerprint(
        _header_lines(FULL_STORY, NAMES)
    )


def test_fingerprint_is_the_same_in_another_process():
    # A pinned value: any process, on any day, must produce exactly this.
    lines = ["Type: feature", "State: Done", "Labels: blog, marketing"]
    assert _fingerprint(lines) == "dcd4ee2c8c"

    # And really in a second interpreter with a different hash seed (Python's
    # built-in hash() would differ here and re-render every story each run).
    code = (
        "from cognee_community_connector_shortcut.shortcut import _fingerprint;"
        f"print('fingerprint=' + _fingerprint({lines!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        check=True,
    )
    assert "fingerprint=dcd4ee2c8c" in result.stdout


# ---------------------------------------------------------------------------
# Listing stories over date windows
# ---------------------------------------------------------------------------
def test_listing_under_the_cap_is_one_request_with_a_half_open_window(fake):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(2))

    stories = _query_stories(fake, {"archived": False}, "created_at", FIRST, LAST)

    assert [story["id"] for story in stories] == [1, 2]
    assert fake.requested(SEARCH) == [
        {
            "archived": False,
            "created_at_start": "2024-03-01T00:00:00Z",
            "created_at_end": "2024-03-28T00:00:00Z",
        }
    ]


def test_window_end_leaves_out_the_rest_of_its_own_second(fake):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(2))

    listed = _query_stories(fake, {}, "updated_at", _parse(at(1)), _parse(at(2)))

    assert [story["id"] for story in listed] == [1]


def test_window_at_the_cap_is_split_until_every_part_is_under_it(fake, monkeypatch):
    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)
    for story_id in range(1, 8):
        fake.add_story(story_id, updated_at=at(story_id))

    stories = _query_stories(fake, {}, "created_at", FIRST, LAST)

    # Every story exactly once, although no single response was trusted.
    assert sorted(story["id"] for story in stories) == [1, 2, 3, 4, 5, 6, 7]
    windows = [(q["created_at_start"], q["created_at_end"]) for q in fake.requested(SEARCH)]
    assert windows[0] == (_stamp(FIRST), _stamp(LAST))
    # The first cut is at the median story (the 4th of 7), not mid-March.
    assert windows[1] == (_stamp(FIRST), at(4))
    # The windows whose result was kept tile the range with no gap or overlap.
    kept = sorted(w for w in windows if sum(w[0] <= at(d) < w[1] for d in range(1, 8)) < 3)
    assert kept[0][0] == _stamp(FIRST) and kept[-1][1] == _stamp(LAST)
    assert all(left[1] == right[0] for left, right in itertools.pairwise(kept))


def test_split_recovers_everything_from_a_server_that_truncates(fake, monkeypatch):
    # The case the guard exists for: a server that silently returns at most N.
    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)
    fake.hard_cap = 3
    for story_id in range(1, 11):
        fake.add_story(story_id, updated_at=at(story_id))

    stories = _query_stories(fake, {}, "updated_at", FIRST, LAST)

    assert sorted(story["id"] for story in stories) == list(range(1, 11))


def test_split_point_is_the_median_and_stays_inside_the_window():
    stories = [{"created_at": at(day)} for day in (2, 9, 5, 3, 4)]
    assert _split_point(stories, "created_at", FIRST, LAST) == _parse(at(4))

    # All stories in the window's first second: cut right after it ...
    same = [{"created_at": _stamp(FIRST)}] * 3
    assert _split_point(same, "created_at", FIRST, LAST) == FIRST + timedelta(seconds=1)
    # ... and never at or past the end, or the right part would be empty.
    late = [{"created_at": _stamp(LAST - timedelta(seconds=1))}] * 3
    cut = _split_point(late, "created_at", LAST - timedelta(seconds=2), LAST)
    assert cut == LAST - timedelta(seconds=1)


def test_story_stamped_exactly_on_a_cut_is_listed_once(fake, monkeypatch):
    # Seen live: both parts of a split include the instant they were cut at.
    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 4)
    for story_id in range(1, 6):
        fake.add_story(story_id, updated_at=at(story_id))
    fake.stories[3]["fraction"] = "000"  # the median, and exactly on the second

    stories = _query_stories(fake, {}, "created_at", FIRST, LAST)

    windows = [(q["created_at_start"], q["created_at_end"]) for q in fake.requested(SEARCH)]
    assert windows[1:] == [(_stamp(FIRST), at(3)), (at(3), _stamp(LAST))]
    assert [story["id"] for story in stories] == [1, 2, 3, 4, 5]  # 3 came back twice


def test_full_window_of_one_second_raises_instead_of_truncating(fake, monkeypatch):
    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)
    fake.add_story(1, updated_at=at(1))
    for story_id in (2, 3, 4):  # e.g. a bulk import: three stories in one second
        fake.add_story(story_id, updated_at=at(2))
    fake.add_story(5, updated_at=at(3))

    with pytest.raises(ShortcutListingError, match="Narrow the selection with group_ids") as err:
        _query_stories(fake, {}, "created_at", FIRST, LAST)
    # Says what came back and from where; it is not an HTTP error.
    assert "returned 3 stories with created_at between" in str(err.value)
    assert "API returned" not in str(err.value)


def test_scope_filters():
    assert _scope_filters(None, None, True) == {}
    assert _scope_filters([TEAM], [7], False) == {
        "group_ids": [TEAM],
        "epic_ids": [7],
        "archived": False,
    }


# ---------------------------------------------------------------------------
# sync_stories: ingest and the cursor
# ---------------------------------------------------------------------------
def test_first_sync_renders_stories_epics_and_iterations(fake):
    fake.add_epic(7)
    fake.add_iteration(3)
    fake.labels[1] = "blog"
    fake.add_story(
        1,
        updated_at=at(2),
        name="Write launch post",
        description="Draft the post.",
        workflow_state_id=IN_PROGRESS,
        owner_ids=[ADA],
        epic_id=7,
        iteration_id=3,
        label_ids=[1],
        comments=[comment(11, GRACE, "Use the new palette.")],
    )
    fake.add_story(2, updated_at=at(1))
    state = {}

    rows = run_sync(fake, state)

    assert [r["id"] for r in rows] == ["story:1", "story:2", "epic:7", "iteration:3"]
    assert rows[0] == {
        "id": "story:1",
        "url": "https://app.shortcut.com/acme/story/1",
        "title": "Write launch post",
        "content": (
            "Type: feature\nState: In Progress\nOwners: Ada Lovelace\nEpic: Website launch\n"
            "Iteration: Sprint 12\nLabels: blog\n\nDraft the post.\n\n"
            "Comments:\n- Grace Hopper: Use the new palette."
        ),
        "_deleted": False,
    }
    assert rows[2]["title"] == "Website launch"
    assert rows[2]["content"] == "State: in progress\n\nShip the new site."
    assert rows[3]["content"] == (
        "Status: started\nStart: 2024-03-04\nEnd: 2024-03-15\n\nTwo weeks of launch work."
    )
    assert state["known_ids"] == ["epic:7", "iteration:3", "story:1", "story:2"]
    assert state["cursor"] == at(2)
    assert set(state["fingerprints"]) == {"1", "2"}
    # A first sync needs no separate updated_at listing: the sweep is one.
    assert len(fake.requested(SEARCH)) == 1


def test_second_sync_without_changes_fetches_no_story(fake):
    fake.add_epic(7)
    fake.add_story(1, updated_at=at(1), epic_id=7)
    fake.add_story(2, updated_at=at(2))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.calls.clear()

    rows = run_sync(fake, state)

    assert story_rows(rows) == {} and tombstones(rows) == []
    assert fake.fetched_stories() == []
    assert [r["id"] for r in rows] == ["epic:7"]  # epics are cheap and re-emitted
    assert state == before
    # The incremental listing asks from 60 seconds before the cursor.
    assert fake.requested(SEARCH)[1]["updated_at_start"] == "2024-03-02T09:59:00Z"


def test_edited_story_is_resynced_and_advances_the_cursor(fake):
    fake.add_story(1, updated_at=at(1), description="v1")
    fake.add_story(2, updated_at=at(2))
    state = {}
    run_sync(fake, state)

    fake.stories[1].update(description="v2", updated_at=at(5))
    fake.calls.clear()
    rows = run_sync(fake, state)

    assert fake.fetched_stories() == [1]
    assert "v2" in story_rows(rows)["story:1"]["content"]
    assert state["cursor"] == at(5)


def test_comment_added_edited_and_deleted_each_rerender_the_story(fake):
    # Verified live: all three move the story's updated_at.
    fake.add_story(1, updated_at=at(1))
    state = {}
    run_sync(fake, state)

    fake.stories[1].update(comments=[comment(11, ADA, "first")], updated_at=at(2))
    assert "- Ada Lovelace: first" in story_rows(run_sync(fake, state))["story:1"]["content"]

    fake.stories[1].update(comments=[comment(11, ADA, "first, edited")], updated_at=at(3))
    assert "first, edited" in story_rows(run_sync(fake, state))["story:1"]["content"]

    fake.stories[1].update(comments=[comment(11, ADA, "gone", deleted=True)], updated_at=at(4))
    assert "Comments" not in story_rows(run_sync(fake, state))["story:1"]["content"]


def test_story_stamped_just_before_the_cursor_is_caught_by_the_overlap(fake):
    fake.add_story(1, updated_at=at(5, second=30))
    state = {}
    run_sync(fake, state)
    assert state["cursor"] == at(5, second=30)

    # Committed late on Shortcut's side with a stamp 20 seconds before the cursor.
    fake.add_story(2, updated_at=at(5, second=10), created_at=at(1))
    state["fingerprints"]["2"] = _fingerprint(["Type: feature", "State: Backlog"])
    state["known_ids"].append("story:2")
    fake.calls.clear()

    rows = run_sync(fake, state)

    # Found by the cursor listing alone: it was already "known" and its header
    # is unchanged, so only the overlap could reveal it. Story 1 is not re-fetched.
    assert fake.fetched_stories() == [2]
    assert set(story_rows(rows)) == {"story:2"}


def test_story_new_to_the_corpus_with_an_old_timestamp_is_ingested(fake):
    fake.add_story(1, updated_at=at(5))
    state = {}
    run_sync(fake, state)

    # Un-archived or moved into scope: updated long before the cursor.
    fake.add_story(2, updated_at=at(1))
    rows = run_sync(fake, state)

    assert set(story_rows(rows)) == {"story:2"}
    assert "story:2" in state["known_ids"]


# ---------------------------------------------------------------------------
# sync_stories: one-second timestamps
# ---------------------------------------------------------------------------
def test_story_fetched_in_the_second_of_its_stamp_is_fetched_once_more(fake):
    fake.add_story(1, updated_at=at(5))
    fake.clock = _parse(at(5))  # Shortcut's clock is still inside that second
    state = {}
    run_sync(fake, state)
    assert state["seen"] == {}  # the copy may not be final for that timestamp

    fake.clock = NOW
    fake.calls.clear()
    run_sync(fake, state)
    assert fake.fetched_stories() == [1]  # fetched again, now well after the second
    assert state["seen"] == {"1": at(5)}

    fake.calls.clear()
    assert story_rows(run_sync(fake, state)) == {}
    assert fake.fetched_stories() == []


def test_second_edit_inside_the_same_second_is_picked_up(fake):
    fake.add_story(1, updated_at=at(5), description="first edit")
    fake.clock = _parse(at(5))
    state = {}
    run_sync(fake, state)

    # Edited again within the same second: updated_at does not move.
    fake.stories[1]["description"] = "second edit"
    fake.clock = NOW
    rows = run_sync(fake, state)

    assert "second edit" in story_rows(rows)["story:1"]["content"]


def test_story_fetched_well_after_its_stamp_is_trusted_at_once(fake):
    fake.add_story(1, updated_at=at(5))
    state = {}
    run_sync(fake, state)  # the fake's clock says NOW, weeks later

    assert state["seen"] == {"1": at(5)}
    fake.calls.clear()
    assert story_rows(run_sync(fake, state)) == {}
    assert fake.fetched_stories() == []


@pytest.mark.parametrize("date_header", [None, "not a date"])
def test_missing_or_unreadable_date_header_means_not_trusted(fake, date_header):
    fake.add_story(1, updated_at=at(5))
    fake.clock = date_header
    state = {}
    run_sync(fake, state)

    assert state["seen"] == {}
    fake.calls.clear()
    run_sync(fake, state)
    assert fake.fetched_stories() == [1]  # costs a request, never misses an edit


def test_is_settled_needs_the_margin_after_the_stamp():
    stamp = at(5, second=10)
    assert not _is_settled(stamp, _parse(at(5, second=10)))
    assert not _is_settled(stamp, _parse(at(5, second=11)))
    assert _is_settled(stamp, _parse(at(5, second=12)))
    assert not _is_settled(stamp, None)
    assert not _is_settled("", NOW)


def test_server_time_reads_the_date_header():
    response = _Resp(200, {}, {"Date": "Fri, 09 Oct 2026 20:30:21 GMT"})
    assert _server_time(response) == datetime(2026, 10, 9, 20, 30, 21, tzinfo=UTC)
    assert _server_time(_Resp(200, {})) is None
    assert _server_time(_Resp(200, {}, {"Date": "soon"})) is None


# ---------------------------------------------------------------------------
# sync_stories: names a story borrows (none of these move updated_at)
# ---------------------------------------------------------------------------
@pytest.fixture
def linked(fake):
    """Story 1 uses an epic, an iteration, a label, a state and an owner; story 2 none."""
    fake.add_epic(7)
    fake.add_iteration(3)
    fake.labels[1] = "blog"
    fake.add_story(1, updated_at=at(1), epic_id=7, iteration_id=3, label_ids=[1], owner_ids=[ADA])
    fake.add_story(2, updated_at=at(2), workflow_state_id=DONE)
    state = {}
    run_sync(fake, state)
    fake.calls.clear()
    return fake, state


def _resync(fake, state):
    rows = run_sync(fake, state)
    return story_rows(rows), rows


def test_renamed_epic_rerenders_its_stories_and_the_epic(linked):
    fake, state = linked
    fake.epics[7]["name"] = "Site relaunch"

    stories, rows = _resync(fake, state)

    assert fake.fetched_stories() == [1]  # story 2 is not in the epic
    assert "Epic: Site relaunch" in stories["story:1"]["content"]
    assert next(r for r in rows if r["id"] == "epic:7")["title"] == "Site relaunch"
    # The new fingerprint is stored: the next run is quiet again.
    fake.calls.clear()
    assert _resync(fake, state)[0] == {} and fake.fetched_stories() == []


def test_deleted_epic_rerenders_its_stories_and_forgets_the_epic(linked):
    fake, state = linked
    fake.delete_epic(7)

    stories, rows = _resync(fake, state)

    assert "Epic:" not in stories["story:1"]["content"]
    assert tombstones(rows) == [{"id": "epic:7", "_deleted": True}]


def test_renamed_iteration_rerenders_its_stories(linked):
    fake, state = linked
    fake.iterations[3].update(name="Sprint 13", updated_at=at(4))  # its own stamp moves

    stories, rows = _resync(fake, state)

    assert "Iteration: Sprint 13" in stories["story:1"]["content"]
    assert fake.fetched_stories() == [1]
    assert next(r for r in rows if r["id"] == "iteration:3")["title"] == "Sprint 13"


def test_renamed_and_deleted_label_rerender_the_stories_that_carry_it(linked):
    fake, state = linked
    fake.labels[1] = "news"
    assert "Labels: news" in _resync(fake, state)[0]["story:1"]["content"]

    fake.delete_label(1)
    assert "Labels" not in _resync(fake, state)[0]["story:1"]["content"]


def test_renamed_workflow_state_rerenders_every_story_in_that_state(linked):
    fake, state = linked
    fake.states[DONE] = "Shipped"

    stories, _ = _resync(fake, state)

    assert fake.fetched_stories() == [2]  # story 1 is in Backlog
    assert "State: Shipped" in stories["story:2"]["content"]


def test_renamed_owner_rerenders_the_stories_they_own(linked):
    fake, state = linked
    fake.members[ADA] = "Ada King"

    assert "Owners: Ada King" in _resync(fake, state)[0]["story:1"]["content"]
    assert fake.fetched_stories() == [1]


def test_story_keeps_the_name_of_an_epic_that_is_not_selected(fake):
    # Scoped to a team: the story is in scope, its epic belongs to nobody.
    fake.add_epic(7, group_ids=[])
    fake.add_story(1, updated_at=at(1), epic_id=7, group_id=TEAM)

    rows = run_sync(fake, {}, group_ids=[TEAM])

    assert [r["id"] for r in rows] == ["story:1"]  # no epic document
    assert "Epic: Website launch" in rows[0]["content"]


# ---------------------------------------------------------------------------
# sync_stories: iterations are fetched only when the list shows a change
# ---------------------------------------------------------------------------
def test_unchanged_iteration_is_not_fetched_again_and_not_forgotten(fake):
    fake.add_iteration(3)
    state = {}
    assert "iteration:3" in [r["id"] for r in run_sync(fake, state)]
    fake.calls.clear()

    rows = run_sync(fake, state)

    assert fake.requested("/iterations/3") == []  # a quiet run costs no request for it
    assert rows == []  # not re-emitted, and above all not hard-deleted
    assert "iteration:3" in state["known_ids"]


def test_edited_iteration_is_fetched_again(fake):
    # Verified live: editing the description, name or dates moves updated_at.
    fake.add_iteration(3)
    state = {}
    run_sync(fake, state)

    fake.iterations[3].update(description="Rewritten.", updated_at=at(5))
    rows = run_sync(fake, state)

    assert [r["id"] for r in rows] == ["iteration:3"]
    assert rows[0]["content"].endswith("Rewritten.")


def test_iteration_whose_status_follows_the_calendar_is_fetched_again(fake):
    # Nobody edits an iteration on its start date, so updated_at stays put.
    fake.add_iteration(3, status="unstarted")
    state = {}
    run_sync(fake, state)

    fake.iterations[3]["status"] = "started"
    rows = run_sync(fake, state)

    assert rows[0]["content"].startswith("Status: started")


def test_iteration_fetched_in_the_second_of_its_stamp_is_fetched_once_more(fake):
    fake.add_iteration(3, updated_at=at(5))
    fake.clock = _parse(at(5))
    state = {}
    run_sync(fake, state)
    assert state["iterations"] == {}

    fake.clock = NOW
    assert [r["id"] for r in run_sync(fake, state)] == ["iteration:3"]
    assert run_sync(fake, state) == []


# ---------------------------------------------------------------------------
# sync_stories: selection, archive, deletion
# ---------------------------------------------------------------------------
def test_selection_filters_stories_epics_and_iterations(fake):
    fake.add_epic(7, group_ids=[TEAM])
    fake.add_epic(8, name="Other team's epic", group_ids=["team-other"])
    fake.add_iteration(3, group_ids=[TEAM])
    fake.add_iteration(4, name="Other sprint", group_ids=["team-other"])
    fake.add_story(1, updated_at=at(1), group_id=TEAM, epic_id=7)
    fake.add_story(2, updated_at=at(1), group_id="team-other", epic_id=8)
    fake.add_story(3, updated_at=at(1), group_id=TEAM, epic_id=8)

    by_team = run_sync(fake, {}, group_ids=[TEAM])
    assert [r["id"] for r in by_team] == ["story:1", "story:3", "epic:7", "iteration:3"]
    assert fake.requested(SEARCH)[0]["group_ids"] == [TEAM]

    by_epic = run_sync(fake, {}, epic_ids=[8])
    assert [r["id"] for r in by_epic] == [
        "story:2",
        "story:3",
        "epic:8",
        "iteration:3",
        "iteration:4",
    ]

    # Both: AND. Epic 8 belongs to the other team, so it gets no document, but
    # the one story of this team inside it is still in scope.
    both = run_sync(fake, {}, group_ids=[TEAM], epic_ids=[8])
    assert [r["id"] for r in both] == ["story:3", "iteration:3"]


def test_archived_story_is_kept_with_its_state(fake):
    fake.add_story(1, updated_at=at(1), workflow_state_id=DONE)
    state = {}
    run_sync(fake, state)

    fake.stories[1].update(archived=True, updated_at=at(2))  # archiving bumps updated_at
    rows = run_sync(fake, state)

    assert story_rows(rows)["story:1"]["content"] == "Type: feature\nState: Done\nArchived: yes"
    assert tombstones(rows) == []
    assert "archived" not in fake.requested(SEARCH)[0]  # no filter: both kinds come back


def test_include_archived_false_skips_and_forgets_archived_stories(fake):
    fake.add_epic(7, archived=True)
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    state = {}
    assert set(story_rows(run_sync(fake, state, include_archived=False))) == {"story:1", "story:2"}
    assert "epic:7" not in state["known_ids"]

    fake.stories[2].update(archived=True, updated_at=at(2))
    rows = run_sync(fake, state, include_archived=False)

    assert tombstones(rows) == [{"id": "story:2", "_deleted": True}]
    assert fake.requested(SEARCH)[0]["archived"] is False


def test_deleted_story_emits_hard_delete_marker(fake):
    fake.add_iteration(3)
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    state = {}
    run_sync(fake, state)

    del fake.stories[2]
    del fake.iterations[3]
    rows = run_sync(fake, state)

    assert tombstones(rows) == [
        {"id": "iteration:3", "_deleted": True},
        {"id": "story:2", "_deleted": True},
    ]
    assert state["known_ids"] == ["story:1"]
    assert set(state["fingerprints"]) == {"1"}

    assert tombstones(run_sync(fake, state)) == []  # emitted once, not on every run


def test_story_deleted_between_sweep_and_fetch_is_forgotten(fake):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    state = {}
    run_sync(fake, state)

    fake.stories[2].update(description="edited", updated_at=at(3))
    fake.queue("/stories/2", _error(404, "Resource not found."))
    rows = run_sync(fake, state)

    assert story_rows(rows) == {}
    assert tombstones(rows) == [{"id": "story:2", "_deleted": True}]
    assert "2" not in state["fingerprints"]


def _fail(fake, path, when=lambda payload: True, status=500):
    """Make ``path`` fail for good: a 5xx is queued once per retry, a 4xx once."""
    for _ in range(shortcut._MAX_RETRIES if status >= 500 else 1):
        fake.queue(path, _error(status, "Server Error"), when=when)


@pytest.mark.parametrize(
    "failing_request",
    ["sweep", "second half of a split sweep", "updated listing", "members", "epics", "a story"],
)
def test_failed_request_deletes_nothing_and_keeps_state(fake, failing_request, monkeypatch):
    # The single most important safety property: a listing that could not be
    # completed must never look like "these stories were deleted".
    for story_id in (1, 2, 3, 4):
        fake.add_story(story_id, updated_at=at(story_id))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)

    del fake.stories[4]  # a real deletion is pending too; it must wait for a clean run
    fake.stories[1].update(description="edited", updated_at=at(9))
    if failing_request == "sweep":
        _fail(fake, SEARCH, when=lambda q: "created_at_start" in q)
    elif failing_request == "second half of a split sweep":
        monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)
        _fail(
            fake,
            SEARCH,
            when=lambda q: (
                q.get("created_at_end") == _stamp(NOW + timedelta(1))
                and q["created_at_start"] != _stamp(shortcut._EPOCH)
            ),
        )
    elif failing_request == "updated listing":
        _fail(fake, SEARCH, when=lambda q: "updated_at_start" in q)
    elif failing_request == "a story":
        _fail(fake, "/stories/1")
    else:
        _fail(fake, f"/{failing_request}", status=403)

    rows = []
    with pytest.raises(ShortcutAPIError):
        for row in sync_stories(fake, state, now=NOW):
            rows.append(row)

    assert tombstones(rows) == []  # never a hard-delete marker
    if failing_request != "a story":
        assert rows == []  # a failed listing emits nothing at all
    assert state == before  # ids, cursor and fingerprints untouched

    # The next clean run picks up exactly where the failed one would have.
    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 2500)
    rows = run_sync(fake, state)
    assert {"id": "story:4", "_deleted": True} in rows
    assert "edited" in story_rows(rows)["story:1"]["content"]


def test_unprovable_listing_aborts_the_sync_without_touching_state(fake, monkeypatch):
    fake.add_story(1, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)

    monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)
    for story_id in (2, 3, 4):
        fake.add_story(story_id, updated_at=at(2))

    with pytest.raises(ShortcutListingError, match="one-second window"):
        run_sync(fake, state)
    assert state == before


# ---------------------------------------------------------------------------
# HTTP: retries
# ---------------------------------------------------------------------------
def test_request_uses_the_v3_base_and_accepts_201(fake):
    fake.add_story(1, updated_at=at(1))
    assert _request(fake, "GET", "/workflows")[0]["name"] == "Standard"
    assert len(_request(fake, "POST", SEARCH, body={"archived": False})) == 1  # answers 201


def test_rate_limit_waits_exactly_retry_after(fake, sleeps):
    fake.queue("/members", _error(429, "Too Many Requests", headers={"Retry-After": "7"}))

    assert len(_request(fake, "GET", "/members")) == 2
    assert sleeps == [7.0]


def test_server_and_network_errors_back_off_then_succeed(sleeps):
    class Flaky(FakeShortcut):
        failures = 1

        def request(self, method, url, **kwargs):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("connection reset")  # an OSError, like requests'
            return super().request(method, url, **kwargs)

    session = Flaky()
    session.queue("/members", _error(503, "Service Unavailable"))

    assert len(_request(session, "GET", "/members")) == 2
    assert sleeps == [1.0, 2.0]  # exponential backoff when there is no Retry-After


def test_retries_are_capped(fake, sleeps):
    _fail(fake, "/members")

    with pytest.raises(ShortcutAPIError, match="500"):
        _request(fake, "GET", "/members")
    assert len(sleeps) == shortcut._MAX_RETRIES - 1


def test_permanent_errors_are_not_retried(fake, sleeps):
    fake.queue("/members", _Resp(401, {"message": "Unauthorized", "tag": "unauthorized"}))
    with pytest.raises(ShortcutAPIError, match="Unauthorized") as excinfo:
        _request(fake, "GET", "/members")
    assert excinfo.value.status == 401
    assert sleeps == []


# ---------------------------------------------------------------------------
# shortcut_source: validation and dlt wiring
# ---------------------------------------------------------------------------
def test_shortcut_source_resource_is_configured_for_merge_and_hard_delete(fake):
    resource = shortcut_source(session=fake)
    assert resource.name == "shortcut_documents"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_shortcut_source_declares_document_marker_and_pipeline_scope(fake):
    source = shortcut_source(session=fake)
    # resolve_dlt_sources routes on this tag: rows become documents for cognify.
    assert SHORTCUT_SOURCE_NAME == "shortcut"
    assert document_source_tag(source) == "shortcut"
    # A scoped pipeline name keeps the cursor apart from other dlt sources.
    assert pipeline_name_for_source(source, "shortcut") != "ingest_dlt_source"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "story:1",
            "url": "https://app.shortcut.com/acme/story/1",
            "title": "Write launch post",
            "content": "Type: feature",
        },
        content_hash="abc123",
        table_name="shortcut_documents",
    )
    data_id = uuid5(NAMESPACE_OID, "story:1")

    item = _build_document_data_item(row, data_id, "shortcut")

    assert item.system_metadata["source"] == "shortcut"
    assert item.system_metadata["url"] == "https://app.shortcut.com/acme/story/1"
    assert item.system_metadata["external_id"] == "story:1"
    assert item.data == "# Write launch post\n\nType: feature"


def test_shortcut_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("SHORTCUT_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SHORTCUT_API_TOKEN"):
        shortcut_source()


def test_shortcut_source_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("SHORTCUT_API_TOKEN", "test-token")
    assert shortcut_source().name == "shortcut_documents"  # no request is made yet


# ---------------------------------------------------------------------------
# dlt pipeline: a real merge into a temp sqlite destination
# ---------------------------------------------------------------------------
def _run_pipeline(tmp_path, session, **source_kwargs):
    import dlt

    db_path = (tmp_path / "shortcut.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="shortcut_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="shortcut_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(shortcut_source(session=session, **source_kwargs))
    return pipeline


def _read_documents(pipeline):
    """Return {id: content} for the shortcut_documents table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM shortcut_documents") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_round_trip_edit_rename_and_delete_across_runs(fake, tmp_path):
    fake.add_epic(7)
    fake.add_story(1, updated_at=at(1), description="v1")
    fake.add_story(2, updated_at=at(2), description="to be deleted")
    fake.add_story(3, updated_at=at(2), description="untouched")
    fake.add_story(4, updated_at=at(2), description="in the epic", epic_id=7)

    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert set(documents) == {"story:1", "story:2", "story:3", "story:4", "epic:7"}
    assert "v1" in documents["story:1"]

    # Between runs: story 1 edited, story 2 deleted, the epic renamed, story 3 untouched.
    fake.stories[1].update(description="v2", updated_at=at(6))
    del fake.stories[2]
    fake.epics[7]["name"] = "Site relaunch"
    fake.calls.clear()

    documents = _read_documents(_run_pipeline(tmp_path, fake))

    # Cursor, id set and fingerprints survived in dlt resource state: only the
    # edited story and the one showing the renamed epic were fetched.
    assert fake.fetched_stories() == [1, 4]
    assert set(documents) == {"story:1", "story:3", "story:4", "epic:7"}  # story 2 hard-deleted
    assert "v2" in documents["story:1"] and "v1" not in documents["story:1"]
    assert "Epic: Site relaunch" in documents["story:4"]
    assert "untouched" in documents["story:3"]

    # A third run with nothing changed fetches no story.
    fake.calls.clear()
    _run_pipeline(tmp_path, fake)
    assert fake.fetched_stories() == []


def test_pipeline_failure_while_rendering_leaves_destination_and_state_untouched(fake, tmp_path):
    fake.add_story(1, updated_at=at(1), description="v1")
    fake.add_story(2, updated_at=at(1), description="v1")
    fake.add_story(3, updated_at=at(1))
    _run_pipeline(tmp_path, fake)

    # Stories 1 and 2 edited, story 3 deleted; the fetch of story 2 keeps failing,
    # after story 1 was already fetched and yielded.
    fake.stories[1].update(description="v2", updated_at=at(5))
    fake.stories[2].update(description="v2", updated_at=at(5))
    del fake.stories[3]
    _fail(fake, "/stories/2")
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_pipeline(tmp_path, fake)

    import dlt

    untouched = dlt.pipeline(
        pipeline_name="shortcut_test",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'shortcut.db').as_posix()}"
        ),
        dataset_name="shortcut_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    documents = _read_documents(untouched)
    # Not even the row that was yielded before the failure reached the destination.
    assert set(documents) == {"story:1", "story:2", "story:3"}
    assert "v1" in documents["story:1"] and "v2" not in documents["story:1"]

    # State was not advanced either: the clean run redoes all of it.
    fake.calls.clear()
    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert fake.fetched_stories() == [1, 2]
    assert set(documents) == {"story:1", "story:2"}
    assert "v2" in documents["story:1"] and "v2" in documents["story:2"]


def test_pipeline_failure_leaves_destination_and_state_untouched(fake, tmp_path):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    _run_pipeline(tmp_path, fake)

    del fake.stories[2]
    fake.queue(SEARCH, _error(403, "Forbidden"))
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_pipeline(tmp_path, fake)

    # The failed run deleted nothing; the following clean run forgets story 2.
    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert set(documents) == {"story:1"}
