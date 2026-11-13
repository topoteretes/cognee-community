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
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_shortcut import shortcut
from cognee_community_connector_shortcut.shortcut import (
    SHORTCUT_SOURCE_NAME,
    ShortcutAPIError,
    ShortcutListingError,
    ShortcutResponseError,
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


def run_sync(session, state, now=NOW, **kwargs):
    return list(sync_stories(session, state, now=now, **kwargs))


def hours(n):
    """The run clock ``n`` hours after ``NOW``, for the give-up rule's 24 hours."""
    return NOW + timedelta(hours=n)


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


def test_shuffled_lists_render_the_same_text():
    # The same story with its owners, labels and comments listed in a different order.
    names = {
        "state": {BACKLOG: "Backlog"},
        "member": {ADA: "Ada Lovelace", GRACE: "Grace Hopper"},
        "epic": {},
        "iteration": {},
    }
    story = {
        "workflow_state_id": BACKLOG,
        "owner_ids": [ADA, GRACE],
        "labels": [{"name": n} for n in ("ui", "bug", "p1")],
        "comments": [comment(10 + i, ADA, f"comment {i}") for i in range(5)],
    }
    expected = _render_story(story, names)

    rng = random.Random(7)
    for _ in range(5):
        shuffled = {
            key: rng.sample(value, len(value)) if isinstance(value, list) else value
            for key, value in story.items()
        }
        assert _render_story(shuffled, names) == expected
    assert expected.index("comment 0") < expected.index("comment 4")  # oldest first


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
    # "-0000" (zone unknown) parses without a zone; it is read as UTC.
    unknown_zone = _Resp(200, {}, {"Date": "Mon, 19 Oct 2026 06:00:00 -0000"})
    assert _server_time(unknown_zone) == datetime(2026, 10, 19, 6, 0, 0, tzinfo=UTC)


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
    fake.add_iteration(5, name="Unplanned sprint", group_ids=[TEAM])
    fake.add_story(1, updated_at=at(1), group_id=TEAM, epic_id=7)
    fake.add_story(2, updated_at=at(1), group_id="team-other", epic_id=8, iteration_id=4)
    fake.add_story(3, updated_at=at(1), group_id=TEAM, epic_id=8, iteration_id=3)

    by_team = run_sync(fake, {}, group_ids=[TEAM])
    assert [r["id"] for r in by_team] == [
        "story:1",
        "story:3",
        "epic:7",
        "iteration:3",
        "iteration:5",
    ]
    assert fake.requested(SEARCH)[0]["group_ids"] == [TEAM]

    # An epic selection takes only the iterations its stories are planned in.
    by_epic = run_sync(fake, {}, epic_ids=[8])
    assert [r["id"] for r in by_epic] == [
        "story:2",
        "story:3",
        "epic:8",
        "iteration:3",
        "iteration:4",
    ]

    # Both: AND. Epic 8 belongs to the other team, so it gets no document, but
    # the one story of this team inside it is still in scope, with its iteration.
    both = run_sync(fake, {}, group_ids=[TEAM], epic_ids=[8])
    assert [r["id"] for r in both] == ["story:3", "iteration:3"]


def test_with_epic_ids_an_iteration_leaves_the_scope_with_its_last_story(fake):
    fake.add_epic(8)
    fake.add_iteration(3)
    fake.add_iteration(4, name="Sprint 13")
    fake.add_story(1, updated_at=at(1), epic_id=8, iteration_id=3)
    state = {}
    rows = run_sync(fake, state, epic_ids=[8])
    assert [r["id"] for r in rows] == ["story:1", "epic:8", "iteration:3"]

    # The story moves to the next sprint. The scope follows it, as it does for a
    # story leaving a selected epic: iteration 3 is forgotten, iteration 4 added.
    fake.stories[1].update(iteration_id=4, updated_at=at(5))
    rows = run_sync(fake, state, epic_ids=[8])
    assert [r["id"] for r in rows if not r["_deleted"]] == ["story:1", "epic:8", "iteration:4"]
    assert tombstones(rows) == [{"id": "iteration:3", "_deleted": True}]


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


@pytest.mark.parametrize("status", [403, 404, 500])
def test_listed_story_whose_get_fails_is_kept_and_retried(fake, status):
    # The sweep lists story 2, but its own GET fails: that does not prove it
    # is gone. Keep the document, retry next run, and let the listing decide.
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    state = {}
    run_sync(fake, state)

    fake.stories[2].update(description="edited", updated_at=at(3))
    # Story 1 moves the cursor six days on, so story 2 falls outside the next
    # run's overlap window: only its "" fingerprint can bring it back.
    fake.stories[1]["updated_at"] = at(9)
    _fail(fake, "/stories/2", status=status)
    rows = run_sync(fake, state)

    assert set(story_rows(rows)) == {"story:1"}  # the rest of the run happened
    assert tombstones(rows) == []
    assert "story:2" in state["known_ids"]
    assert state["fingerprints"]["2"] == ""
    assert "2" not in state["seen"]

    fake.calls.clear()
    rows = run_sync(fake, state)  # Shortcut recovered
    assert fake.fetched_stories() == [2]
    assert "edited" in story_rows(rows)["story:2"]["content"]
    assert state["fingerprints"]["2"] != ""


def test_listed_iteration_whose_get_fails_is_kept_and_retried(fake):
    fake.add_iteration(3)
    state = {}
    run_sync(fake, state)

    fake.iterations[3].update(description="Rewritten.", updated_at=at(5))
    fake.queue("/iterations/3", _error(404, "Resource not found."))
    rows = run_sync(fake, state)

    assert rows == []  # not re-emitted, and above all not hard-deleted
    assert "iteration:3" in state["known_ids"]
    assert "3" not in state["iterations"]  # no mark: asked for again next run

    rows = run_sync(fake, state)
    assert rows[0]["content"].endswith("Rewritten.")


@pytest.mark.parametrize("failure", ["401", "rate limit", "long retry-after", "outage"])
def test_systemic_item_failure_stops_cleanly_and_resumes(fake, failure, sleeps):
    for story_id in (1, 2, 3, 4, 5):
        fake.add_story(story_id, updated_at=at(1), description="v1")
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2, 3, 4, 5):
        fake.stories[story_id].update(description="v2", updated_at=at(5))
    del fake.stories[5]  # a real deletion waits for a run that finishes

    if failure == "401":
        fake.queue("/stories/3", _error(401, "Unauthorized"))
    elif failure == "rate limit":
        for _ in range(shortcut._MAX_RETRIES):
            fake.queue("/stories/3", _error(429, "Too Many Requests"))
    elif failure == "long retry-after":
        fake.queue("/stories/3", _error(429, "Too Many Requests", headers={"Retry-After": "120"}))
    else:
        for story_id in (2, 3, 4):
            _fail(fake, f"/stories/{story_id}")
    stats = {}
    rows = run_sync(fake, state, stats=stats)

    reasons = {"401": "auth", "rate limit": "rate_limit", "long retry-after": "rate_limit"}
    assert stats[f"failed_{reasons.get(failure, 'unavailable')}"] == 1
    assert all(wait <= shortcut._MAX_WAIT_SECONDS for wait in sleeps)
    assert tombstones(rows) == []  # nothing deleted on a stopped run
    done = {"story:1"} if failure == "outage" else {"story:1", "story:2"}
    assert set(story_rows(rows)) == done
    assert "story:5" in state["known_ids"]
    assert state["cursor"] == at(1)  # kept: the next run lists the same window again

    # Next run, with no upstream change: the rest is rendered and story 5 forgotten.
    fake.calls.clear()
    rows = run_sync(fake, state)
    rest = {"story:2", "story:3", "story:4"} if failure == "outage" else {"story:3", "story:4"}
    assert set(story_rows(rows)) == rest
    assert all("v2" in row["content"] for row in story_rows(rows).values())
    assert tombstones(rows) == [{"id": "story:5", "_deleted": True}]
    assert len(fake.requested(SEARCH)) == 2  # the sweep and one updated listing


def test_first_sync_stopped_part_way_does_not_fetch_the_rendered_stories_again(fake):
    for story_id in (1, 2, 3, 4, 5):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    fake.queue("/stories/3", _error(401, "Unauthorized"))
    rows = run_sync(fake, state)
    assert set(story_rows(rows)) == {"story:1", "story:2"}

    fake.calls.clear()
    rows = run_sync(fake, state)
    assert sorted(set(fake.fetched_stories())) == [3, 4, 5]
    assert set(story_rows(rows)) == {"story:3", "story:4", "story:5"}


def test_401_before_anything_was_rendered_raises(fake):
    fake.add_story(1, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.stories[1]["updated_at"] = at(5)
    fake.queue("/stories/1", _error(401, "Unauthorized"))

    with pytest.raises(ShortcutAPIError) as excinfo:
        run_sync(fake, state)
    assert excinfo.value.status == 401
    assert state == before


def test_three_unreadable_stories_never_block_the_run_and_are_given_up(fake):
    # Stories 11, 12 and 13 always answer 404 while still listed, next to each other.
    for story_id in (10, 11, 12, 13, 14):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (10, 11, 12, 13, 14):
        fake.stories[story_id]["updated_at"] = at(5)

    def forbid():
        for story_id in (11, 12, 13):
            fake.queue(f"/stories/{story_id}", _error(404, "Resource not found."))

    for run in (1, 2):
        forbid()
        stats = {}
        rows = run_sync(fake, state, now=hours(12 * run), stats=stats)
        if run == 1:
            assert set(story_rows(rows)) == {"story:10", "story:14"}  # the others went on
        else:
            assert stats["probe_story"] == 1  # 10 and 14 are unchanged: one is read as a check
        assert tombstones(rows) == []
        assert state["retry"] == ["story:11", "story:12", "story:13"]
        runs = {row_id: entry["runs"] for row_id, entry in state["unreadable"].items()}
        assert runs == {"story:11": run, "story:12": run, "story:13": run}
        assert stats["kept"] == 3 and "failed_unavailable" not in stats
        assert "failed_permission" not in stats

    # A story created upstream meanwhile still gets its first document.
    fake.add_story(15, updated_at=at(6))
    forbid()
    stats = {}
    rows = run_sync(fake, state, now=hours(36), stats=stats)
    assert "story:15" in story_rows(rows)
    # Third run, 24 hours after the first: forgotten, and no longer asked about.
    assert [r["id"] for r in tombstones(rows)] == ["story:11", "story:12", "story:13"]
    assert stats["given_up"] == 3 and state["retry"] == [] and state["unreadable"] == {}
    fake.calls.clear()
    run_sync(fake, state, now=hours(48))
    assert 11 not in fake.fetched_stories()

    # Once a given-up story changes upstream, it is asked about (and ingested) again.
    fake.stories[12]["updated_at"] = at(7)
    rows = run_sync(fake, state, now=hours(48))
    assert set(story_rows(rows)) == {"story:12"}
    assert "story:12" not in state["given_up"]


def test_failing_runs_within_a_day_forget_nothing_and_a_recovery_clears_them(fake):
    # A host that syncs every ten minutes sees four runs of 404s in under an hour.
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)

    for minutes in (10, 20, 30, 40):
        fake.queue("/stories/2", _error(404, "Resource not found."))
        rows = run_sync(fake, state, now=NOW + timedelta(minutes=minutes))
        assert tombstones(rows) == []
    assert state["unreadable"]["story:2"]["runs"] == 4
    assert state["given_up"] == {}

    # Shortcut recovers: the story is read again and its failures are forgotten.
    rows = run_sync(fake, state, now=hours(1))
    assert set(story_rows(rows)) == {"story:2"}
    assert state["unreadable"] == {} and state["retry"] == []


def test_a_partial_outage_answering_404_everywhere_forgets_nothing(fake, caplog):
    # Every GET /stories/N answers 404, the check of an unchanged story included.
    for story_id in (1, 2, 3, 4):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2, 3):
        fake.stories[story_id]["updated_at"] = at(5)

    for run in (1, 2, 3, 4):
        for story_id in (1, 2, 3, 4):
            fake.queue(f"/stories/{story_id}", _error(404, "Resource not found."))
        stats = {}
        rows = run_sync(fake, state, now=hours(24 * run), stats=stats)
        assert tombstones(rows) == []
        assert stats["probe_story"] == 1 and stats["given_up"] == 0
        assert stats["failed_permission"] == 3
        assert state["unreadable"] == {}
    assert "story: every fetch answered 403/404 this run" in caplog.text
    assert {"story:1", "story:2", "story:3", "story:4"} <= set(state["known_ids"])


def test_a_lost_permission_never_gives_anything_up(fake, caplog):
    # Every story answers 403 and there is no other story to compare with: kept,
    # never forgotten, and the token's permissions are named.
    for story_id in (1, 2, 3):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2, 3):
        fake.stories[story_id]["updated_at"] = at(5)

    for run in (1, 2, 3, 4):
        for story_id in (1, 2, 3):
            fake.queue(f"/stories/{story_id}", _error(403, "Forbidden"))
        stats = {}
        rows = run_sync(fake, state, now=hours(24 * run), stats=stats)
        assert tombstones(rows) == []
        assert stats["failed_permission"] == 3 and stats["given_up"] == 0
        assert "probe_story" not in stats
        assert state["unreadable"] == {}
    assert "there is no other story to check access with" in caplog.text
    assert {"story:1", "story:2", "story:3"} <= set(state["known_ids"])


def test_one_hidden_story_among_unchanged_ones_is_given_up(fake, caplog):
    # Steady state: story 1 is unchanged and skipped, so only story 2 is fetched,
    # and story 1 is read once per run to show that access works.
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)

    for run in (1, 2, 3):
        fake.queue("/stories/2", _error(403, "Forbidden"))
        fake.calls.clear()
        stats = {}
        rows = run_sync(fake, state, now=hours(12 * run), stats=stats)
        assert fake.fetched_stories() == [2, 1]  # the check comes after the failure
        assert stats["probe_story"] == 1 and "failed_permission" not in stats
        assert story_rows(rows) == {}  # the check is not emitted
    assert [r["id"] for r in tombstones(rows)] == ["story:2"]
    assert "check the token's permissions" not in caplog.text


def test_a_hidden_iteration_is_checked_against_an_unchanged_one(fake):
    fake.add_iteration(3)
    fake.add_iteration(4, name="Sprint 13")
    state = {}
    run_sync(fake, state)
    fake.iterations[4]["updated_at"] = at(5)

    for run in (1, 2, 3):
        fake.queue("/iterations/4", _error(403, "Forbidden"))
        stats = {}
        rows = run_sync(fake, state, now=hours(12 * run), stats=stats)
        assert stats["probe_iteration"] == 1 and "failed_permission" not in stats
    assert [r["id"] for r in tombstones(rows)] == ["iteration:4"]


@pytest.mark.parametrize("check", ["403", "500"])
def test_a_run_that_counted_nothing_gives_nothing_up(fake, caplog, check):
    # Story 2 is counted three times within an hour (a fast cron), so the 24 hours
    # hold it back. A day later the token loses access to stories: the check fails
    # too, and that run must not give story 2 up on its old count.
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)
    for minutes in (10, 20, 30):
        fake.queue("/stories/2", _error(404, "Resource not found."))
        run_sync(fake, state, now=NOW + timedelta(minutes=minutes))
    assert state["unreadable"]["story:2"]["runs"] == 3

    fake.queue("/stories/2", _error(403, "Forbidden"))
    if check == "403":
        fake.queue("/stories/1", _error(403, "Forbidden"))
    else:
        _fail(fake, "/stories/1")
    stats = {}
    rows = run_sync(fake, state, now=hours(25), stats=stats)

    assert tombstones(rows) == [] and stats["given_up"] == 0
    assert state["given_up"] == {} and state["unreadable"]["story:2"]["runs"] == 3
    assert ("failed_permission" in stats) == (check == "403")


def test_state_written_by_the_previous_version_is_read(fake):
    # Before this version, given_up held a bare stamp and unreadable had no since.
    for story_id in (1, 2, 3, 4):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (2, 3):
        fake.stories[story_id]["updated_at"] = at(5)
    state["given_up"] = {"story:4": at(1)}
    state["known_ids"].remove("story:4")
    state["unreadable"] = {
        "story:2": {"stamp": at(5), "runs": 2},
        "story:3": {"stamp": at(5), "runs": 0},
    }
    state["retry"] = ["story:2", "story:3"]
    fake.queue("/stories/2", _error(404, "Resource not found."))

    rows = run_sync(fake, state, now=hours(1))

    assert set(story_rows(rows)) == {"story:3", "story:4"}  # 4 was due to be asked again
    assert state["given_up"] == {}
    assert state["unreadable"] == {
        "story:2": {"stamp": at(5), "runs": 3, "since": _stamp(hours(1))}
    }


def test_a_check_answering_a_server_error_counts_nothing(fake, caplog):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)
    fake.queue("/stories/2", _error(403, "Forbidden"))
    _fail(fake, "/stories/1")  # the check: Shortcut is failing, which proves nothing
    stats = {}

    rows = run_sync(fake, state, now=hours(12), stats=stats)

    assert tombstones(rows) == []
    assert stats["probe_story"] == 1 and "failed_permission" not in stats
    assert state["unreadable"] == {} and state["retry"] == ["story:2"]
    assert "check the token's permissions" not in caplog.text


def test_a_given_up_story_is_asked_again_after_a_day(fake):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)
    for run in (1, 2, 3):
        fake.queue("/stories/2", _error(404, "Resource not found."))
        run_sync(fake, state, now=hours(12 * run))
    assert state["given_up"] == {"story:2": {"stamp": at(5), "at": _stamp(hours(36))}}

    # Not asked within the day; asked a day later, and still unreadable.
    fake.calls.clear()
    run_sync(fake, state, now=hours(59))
    assert 2 not in fake.fetched_stories()
    fake.queue("/stories/2", _error(404, "Resource not found."))
    stats = {}
    rows = run_sync(fake, state, now=hours(60), stats=stats)
    assert 2 in fake.fetched_stories() and rows == []
    # Its document is already gone: no failure of the run, but counted and named.
    assert stats["failed"] == 0 and stats["reask_failed"] == 1 and stats["kept"] == 0
    assert state["given_up"]["story:2"]["at"] == _stamp(hours(60))
    assert state["retry"] == [] and state["unreadable"] == {}

    # Readable again a day later, though never edited: it comes back, and stays.
    rows = run_sync(fake, state, now=hours(84))
    assert set(story_rows(rows)) == {"story:2"}
    assert state["given_up"] == {} and "story:2" in state["known_ids"]
    assert tombstones(run_sync(fake, state, now=hours(85))) == []


def test_every_given_up_item_that_is_due_is_asked_again_in_one_run(fake):
    for story_id in range(1, 27):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    # Stories 2 to 26 were forgotten two days ago (or by the previous version,
    # which wrote a bare stamp); all of them read again.
    state["given_up"] = {
        f"story:{i}": {"stamp": at(1), "at": _stamp(hours(-48))} for i in range(2, 26)
    } | {"story:26": at(1)}
    state["known_ids"] = ["story:1"]
    for i in range(2, 27):
        del state["fingerprints"][str(i)]
    fake.add_story(30, updated_at=at(6))
    fake.calls.clear()

    rows = run_sync(fake, state)

    assert fake.fetched_stories() == [30, *range(2, 27)]  # new stories first
    assert set(story_rows(rows)) == {f"story:{i}" for i in (*range(2, 27), 30)}
    assert state["given_up"] == {}


def test_a_run_that_stops_early_counts_nothing_toward_giving_up(fake):
    for story_id in (1, 2, 3):
        fake.add_story(story_id, updated_at=at(1))
    fake.add_iteration(9)
    state = {}
    run_sync(fake, state)
    fake.stories[2]["updated_at"] = at(5)
    for run in (1, 2):
        fake.queue("/stories/2", _error(403, "Forbidden"))
        run_sync(fake, state, now=hours(12 * run))
    unreadable = copy.deepcopy(state["unreadable"])
    assert unreadable["story:2"]["runs"] == 2
    # Story 3 was forgotten a day ago and is due to be asked again.
    state["given_up"] = {"story:3": {"stamp": at(1), "at": _stamp(hours(24))}}
    state["known_ids"].remove("story:3")
    del state["fingerprints"]["3"]

    # Run 3: story 1 is read, story 2 answers 403 again, story 3 is still hidden,
    # then the token is revoked on the iteration and the run stops.
    fake.stories[1]["updated_at"] = at(6)
    fake.iterations[9]["updated_at"] = at(6)
    fake.queue("/stories/2", _error(403, "Forbidden"))
    fake.queue("/stories/3", _error(404, "Resource not found."))
    fake.queue("/iterations/9", _error(401, "Unauthorized"))
    stats = {}
    rows = run_sync(fake, state, now=hours(48), stats=stats)

    assert stats["failed_auth"] == 1 and tombstones(rows) == []
    assert state["unreadable"] == unreadable  # not a third run
    assert state["given_up"] == {"story:3": {"stamp": at(1), "at": _stamp(hours(24))}}


def test_retries_go_last_and_never_count_toward_an_outage(fake):
    for story_id in (1, 2, 3, 4, 5, 6, 7):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (2, 3, 4, 5, 6):
        fake.stories[story_id]["updated_at"] = at(5)
    for story_id in (2, 4, 6):  # kept after a 500, with successes between them
        _fail(fake, f"/stories/{story_id}")
    run_sync(fake, state)
    assert state["retry"] == ["story:2", "story:4", "story:6"]

    # Still failing next run, three in a row at the end, after a fresh story.
    fake.stories[7]["updated_at"] = at(6)
    for story_id in (2, 4, 6):
        _fail(fake, f"/stories/{story_id}")
    fake.calls.clear()
    stats = {}
    rows = run_sync(fake, state, stats=stats)

    assert list(dict.fromkeys(fake.fetched_stories())) == [7, 2, 4, 6]
    assert set(story_rows(rows)) == {"story:7"}
    assert "failed_unavailable" not in stats and stats["failed"] == 3


def test_a_success_between_server_errors_resets_the_outage_streak(fake):
    for story_id in (1, 2, 3, 4, 5):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2, 3, 4, 5):
        fake.stories[story_id]["updated_at"] = at(5)
    for story_id in (1, 2, 4, 5):  # 3 succeeds in between
        _fail(fake, f"/stories/{story_id}")
    stats = {}

    rows = run_sync(fake, state, stats=stats)

    assert set(story_rows(rows)) == {"story:3"}
    assert "failed_unavailable" not in stats
    assert state["retry"] == ["story:1", "story:2", "story:4", "story:5"]


def test_story_body_of_the_wrong_shape_aborts(fake):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    fake.stories[2]["updated_at"] = at(5)
    fake.queue("/stories/2", _Resp(200, "<html>login</html>"))  # a login page with a 200

    with pytest.raises(ShortcutResponseError):
        run_sync(fake, state)
    assert state == before


def test_two_failures_in_a_row_are_kept_and_a_success_resets_the_streak(fake):
    for story_id in (1, 2, 3, 4, 5):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2, 3, 4, 5):
        fake.stories[story_id]["updated_at"] = at(5)
    for story_id in (1, 2, 4, 5):  # 3 succeeds in between
        _fail(fake, f"/stories/{story_id}", status=404)

    rows = run_sync(fake, state)

    assert set(story_rows(rows)) == {"story:3"}
    assert tombstones(rows) == []
    assert {k: state["fingerprints"][k] for k in "1245"} == dict.fromkeys("1245", "")


@pytest.mark.parametrize("where", ["sweep", "second half of a split sweep", "updated listing"])
@pytest.mark.parametrize("body", ["an object", "not JSON"])
def test_story_listing_of_the_wrong_shape_raises(fake, monkeypatch, where, body):
    # Never read as an empty window, least of all on the split path, where an
    # empty half would silently drop every story stamped in it.
    for story_id in (1, 2, 3, 4):
        fake.add_story(story_id, updated_at=at(story_id))
    state = {}
    run_sync(fake, state)
    before = copy.deepcopy(state)
    del fake.stories[4]

    response = _Resp(201, {"data": [], "next": None})
    if body == "not JSON":

        def not_json():
            raise ValueError("Expecting value")

        response.json = not_json
    if where == "sweep":
        when = lambda q: "created_at_start" in q  # noqa: E731
    elif where == "updated listing":
        when = lambda q: "updated_at_start" in q  # noqa: E731
    else:
        monkeypatch.setattr(shortcut, "_WINDOW_CAP", 3)

        def when(q):
            return q.get("created_at_end") == _stamp(NOW) and q["created_at_start"] != _stamp(
                shortcut._EPOCH
            )

    fake.queue(SEARCH, response, when=when)

    with pytest.raises(ShortcutResponseError):
        run_sync(fake, state)
    assert state == before


def _fail(fake, path, when=lambda payload: True, status=500):
    """Make ``path`` fail for good: a 5xx is queued once per retry, a 4xx once."""
    for _ in range(shortcut._MAX_RETRIES if status >= 500 else 1):
        fake.queue(path, _error(status, "Server Error"), when=when)


@pytest.mark.parametrize(
    "failing_request",
    ["sweep", "second half of a split sweep", "updated listing", "members", "epics"],
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
                q.get("created_at_end") == _stamp(NOW)
                and q["created_at_start"] != _stamp(shortcut._EPOCH)
            ),
        )
    elif failing_request == "updated listing":
        _fail(fake, SEARCH, when=lambda q: "updated_at_start" in q)
    else:
        _fail(fake, f"/{failing_request}", status=403)

    rows = []
    with pytest.raises(ShortcutAPIError):
        for row in sync_stories(fake, state, now=NOW):
            rows.append(row)

    assert rows == []  # a failed listing emits nothing at all, no hard-delete marker
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
    assert _request(fake, "GET", "/workflows", list)[0]["name"] == "Standard"
    assert len(_request(fake, "POST", SEARCH, list, body={"archived": False})) == 1  # answers 201


def test_rate_limit_waits_exactly_retry_after(fake, sleeps):
    fake.queue("/members", _error(429, "Too Many Requests", headers={"Retry-After": "7"}))

    assert len(_request(fake, "GET", "/members", list)) == 2
    assert sleeps == [7.0]


def test_server_and_network_errors_back_off_then_succeed(sleeps):
    class Flaky(FakeShortcut):
        failures = 1

        def request(self, method, url, **kwargs):
            if self.failures:
                self.failures -= 1
                raise requests.ConnectionError("connection reset")
            return super().request(method, url, **kwargs)

    session = Flaky()
    session.queue("/members", _error(503, "Service Unavailable"))

    assert len(_request(session, "GET", "/members", list)) == 2
    assert sleeps == [1.0, 2.0]  # exponential backoff when there is no Retry-After


def test_retries_are_capped(fake, sleeps):
    _fail(fake, "/members")

    with pytest.raises(ShortcutAPIError, match="500"):
        _request(fake, "GET", "/members", list)
    assert len(sleeps) == shortcut._MAX_RETRIES - 1


def test_permanent_errors_are_not_retried(fake, sleeps):
    fake.queue("/members", _Resp(401, {"message": "Unauthorized", "tag": "unauthorized"}))
    with pytest.raises(ShortcutAPIError) as excinfo:
        _request(fake, "GET", "/members", list)
    assert excinfo.value.status == 401
    assert str(excinfo.value) == "Shortcut /members: HTTP 401"
    assert excinfo.value.detail == "Unauthorized"
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


def test_resource_name_names_the_table_and_scopes_the_state(fake):
    default = shortcut_source(session=fake)
    work = shortcut_source(session=fake, resource_name="shortcut_work")

    assert work.name == "shortcut_work"
    assert pipeline_name_for_source(work, "s") != pipeline_name_for_source(default, "s")


def test_shortcut_source_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("SHORTCUT_API_TOKEN", "test-token")
    assert shortcut_source().name == "shortcut_documents"  # no request is made yet


# ---------------------------------------------------------------------------
# dlt pipeline: a real merge into a temp sqlite destination
# ---------------------------------------------------------------------------
def _pipeline(tmp_path, name):
    import dlt

    db_path = (tmp_path / "shortcut.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="shortcut_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _scope_name(**source_kwargs):
    """The dlt pipeline cognee would use for this source in dataset ``shortcut_ds``."""
    source = shortcut_source(session=FakeShortcut(), **source_kwargs)
    return pipeline_name_for_source(source, "shortcut_ds")


def _run_pipeline(tmp_path, session, **source_kwargs):
    source = shortcut_source(session=session, **source_kwargs)
    # One dlt pipeline per source scope, the way cognee names them.
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "shortcut_ds"))
    pipeline.run(source)
    return pipeline


def _read_documents(pipeline, table="shortcut_documents"):
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


def test_pipeline_deletion_removes_the_row_from_the_destination(fake, tmp_path):
    # Goes red if the _deleted marker is not emitted or hard_delete is off:
    # the row must be gone, not kept emptied or with _deleted set.
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    _run_pipeline(tmp_path, fake)

    del fake.stories[2]
    pipeline = _run_pipeline(tmp_path, fake)

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT COUNT(*) FROM shortcut_documents WHERE id = 'story:2'"
        ) as cursor,
    ):
        assert cursor.fetchone()[0] == 0
    assert "story:1" in _read_documents(pipeline)


def test_pipeline_two_sources_in_one_dataset_stay_apart(fake, tmp_path):
    fake.add_epic(7, name="Site")
    fake.add_epic(8, name="Blog")
    fake.add_story(1, updated_at=at(1), epic_id=7)
    fake.add_story(2, updated_at=at(1), epic_id=8)
    site = {"epic_ids": [7], "resource_name": "shortcut_site"}
    blog = {"epic_ids": [8], "resource_name": "shortcut_blog"}

    _run_pipeline(tmp_path, fake, **site)
    _run_pipeline(tmp_path, fake, **blog)
    site_docs = _read_documents(_run_pipeline(tmp_path, fake, **site), "shortcut_site")
    blog_docs = _read_documents(_run_pipeline(tmp_path, fake, **blog), "shortcut_blog")

    assert set(site_docs) == {"story:1", "epic:7"}
    assert set(blog_docs) == {"story:2", "epic:8"}

    # A deletion in one selection is forgotten there and leaves the other alone.
    del fake.stories[1]
    site_docs = _read_documents(_run_pipeline(tmp_path, fake, **site), "shortcut_site")
    blog_docs = _read_documents(_run_pipeline(tmp_path, fake, **blog), "shortcut_blog")
    assert set(site_docs) == {"epic:7"}
    assert set(blog_docs) == {"story:2", "epic:8"}


def test_check_active_is_asked_before_every_request(fake):
    fake.add_story(1, updated_at=at(1))
    fake.add_iteration(3)
    checks = []
    source = shortcut_source(session=fake, check_active=lambda: checks.append(len(fake.calls)))
    assert list(source)
    # Asked before each request (and around each row), so never behind it.
    assert sorted(set(checks)) == list(range(len(fake.calls) + 1))


@pytest.mark.parametrize("stop_after_requests", [1, 5, 7])
def test_check_active_raising_stops_with_nothing_deleted_or_loaded(
    fake, tmp_path, stop_after_requests
):
    fake.add_story(1, updated_at=at(1), description="v1")
    fake.add_story(2, updated_at=at(1))
    _run_pipeline(tmp_path, fake)
    before = _read_documents(_pipeline(tmp_path, _scope_name()))

    del fake.stories[2]
    fake.stories[1].update(description="v2", updated_at=at(4))
    fake.calls.clear()

    class AccessRevokedError(Exception):
        pass

    def check_active():
        if len(fake.calls) >= stop_after_requests:
            raise AccessRevokedError

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, check_active=check_active)
    assert any(isinstance(error, AccessRevokedError) for error in _chain(excinfo.value))
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before

    # The state did not move: the next run does the deletion and the edit.
    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert set(documents) == {"story:1"}
    assert "v2" in documents["story:1"]


def test_check_active_raising_an_oserror_is_not_retried_or_swallowed(fake, tmp_path, sleeps):
    # PermissionError is an OSError; only requests' own errors are retried.
    fake.add_story(1, updated_at=at(1))
    _run_pipeline(tmp_path, fake)
    before = _read_documents(_pipeline(tmp_path, _scope_name()))
    fake.stories[1].update(description="v2", updated_at=at(4))
    fake.calls.clear()

    def check_active():
        if fake.fetched_stories():  # the story's own GET has been made
            raise PermissionError("access revoked")

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake, check_active=check_active)
    assert any(isinstance(error, PermissionError) for error in _chain(excinfo.value))
    assert sleeps == []  # not retried
    assert _read_documents(_pipeline(tmp_path, _scope_name())) == before


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


def test_pipeline_story_that_fails_on_its_own_is_kept_and_retried(fake, tmp_path):
    fake.add_story(1, updated_at=at(1), description="v1")
    fake.add_story(2, updated_at=at(1), description="v1")
    old = _read_documents(_run_pipeline(tmp_path, fake))["story:2"]

    # Story 1 moves the cursor past story 2's overlap window, so only the ""
    # fingerprint can bring story 2 back on the next run.
    fake.stories[1].update(description="v2", updated_at=at(9))
    fake.stories[2].update(description="v2", updated_at=at(4))
    _fail(fake, "/stories/2")  # a persistent 500 on story 2
    documents = _read_documents(_run_pipeline(tmp_path, fake))

    assert "v2" in documents["story:1"]  # the run completed
    assert documents["story:2"] == old  # kept as it was, not deleted

    fake.calls.clear()
    documents = _read_documents(_run_pipeline(tmp_path, fake))  # Shortcut recovered
    assert fake.fetched_stories() == [2]
    assert "v2" in documents["story:2"]


def test_pipeline_401_mid_run_commits_progress_and_resumes(fake, tmp_path):
    # A token that stops working on story 3 of 5: stories 1-2 are kept, 3-5 come next run.
    for story_id in (1, 2, 3, 4, 5, 6):
        fake.add_story(story_id, updated_at=at(1), description="v1")
    _run_pipeline(tmp_path, fake)
    for story_id in (1, 2, 3, 4, 5):
        fake.stories[story_id].update(description="v2", updated_at=at(5))
    del fake.stories[6]
    fake.queue("/stories/3", _error(401, "Unauthorized"))

    source = shortcut_source(session=fake)
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "shortcut_ds"))
    pipeline.run(source)
    documents = _read_documents(pipeline)
    assert source.cognee_sync_stats["failed_auth"] == 1
    assert "v2" in documents["story:1"] and "v2" in documents["story:2"]
    assert "v1" in documents["story:3"]
    assert "story:6" in documents  # no deletion on a stopped run

    fake.calls.clear()  # no upstream change before the next run
    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert sorted(set(fake.fetched_stories())) == [3, 4, 5]
    assert all("v2" in documents[f"story:{story_id}"] for story_id in (1, 2, 3, 4, 5))
    assert "story:6" not in documents


def test_pipeline_failure_leaves_destination_and_state_untouched(fake, tmp_path):
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    _run_pipeline(tmp_path, fake)

    del fake.stories[2]
    fake.queue(SEARCH, _error(403, "Forbidden"))
    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, fake)
    assert any(
        isinstance(error, ShortcutAPIError) and error.status == 403
        for error in _chain(excinfo.value)
    )

    # The failed run deleted nothing; the following clean run forgets story 2.
    documents = _read_documents(_run_pipeline(tmp_path, fake))
    assert set(documents) == {"story:1"}


# ---------------------------------------------------------------------------
# Review round 2: clock, secrets, wiring
# ---------------------------------------------------------------------------
def test_sweep_ends_at_shortcuts_clock_not_the_local_one(fake, monkeypatch):
    fake.add_story(1, updated_at=at(1))
    state = {}
    list(sync_stories(fake, state))
    # The local clock runs two days behind Shortcut's: a story created since must
    # still be in the sweep, and nothing known may look deleted.
    monkeypatch.setattr(shortcut, "_local_now", lambda: NOW - timedelta(days=2))
    fake.clock = NOW
    fake.add_story(2, updated_at="2024-03-27T12:00:00Z")

    rows = list(sync_stories(fake, state))

    assert set(story_rows(rows)) == {"story:2"}
    assert tombstones(rows) == []
    assert fake.requested(SEARCH)[-2]["created_at_end"] == _stamp(NOW)


def test_without_a_date_header_the_local_clock_ends_the_sweep_and_is_logged(
    fake, monkeypatch, caplog
):
    fake.add_story(1, updated_at=at(1))
    fake.clock = None
    monkeypatch.setattr(shortcut, "_local_now", lambda: NOW)

    rows = list(sync_stories(fake, {}))

    assert set(story_rows(rows)) == {"story:1"}
    assert "no Date header" in caplog.text


def test_a_stale_date_header_cannot_end_the_sweep_early(fake, monkeypatch, caplog):
    # Story 9 was created yesterday evening and is known. A proxy then serves a
    # Date a day old: the sweep must still reach story 9, so nothing looks deleted.
    monkeypatch.setattr(shortcut, "_local_now", lambda: NOW)
    fake.add_story(1, updated_at=at(1))
    fake.add_story(9, updated_at="2024-03-27T12:00:00Z")
    state = {}
    list(sync_stories(fake, state))
    fake.clock = NOW - timedelta(days=1)

    rows = list(sync_stories(fake, state))

    assert tombstones(rows) == []
    assert fake.requested(SEARCH)[-2]["created_at_end"] == _stamp(NOW)
    assert "differ by 86400s" in caplog.text


SECRET = "SECRET-TOKEN-123"


def test_token_with_a_trailing_newline_is_refused_without_echoing_it():
    with pytest.raises(ValueError) as excinfo:
        shortcut_source(token=SECRET + "\n")
    assert SECRET not in str(excinfo.value)
    assert "whitespace" in str(excinfo.value)


def test_the_token_is_never_in_an_error_message_or_a_log_line(fake, caplog, sleeps):
    class Leaky(FakeShortcut):
        def request(self, method, url, **kwargs):
            if url.endswith("/members"):
                raise requests.exceptions.InvalidHeader(f"Invalid header value b'{SECRET}'")
            return super().request(method, url, **kwargs)

    with pytest.raises(ShortcutAPIError) as excinfo:
        _request(Leaky(), "GET", "/members", list)
    assert SECRET not in str(excinfo.value) and excinfo.value.status == 0

    # A 401 whose body echoes the token: neither the message nor the logs carry it.
    fake.headers = {"Shortcut-Token": SECRET}
    fake.queue("/epics", _Resp(401, {"message": f"bad token abcd-{SECRET}"}))
    with pytest.raises(ShortcutAPIError) as excinfo:
        run_sync(fake, {})
    assert str(excinfo.value) == "Shortcut /epics: HTTP 401"
    assert excinfo.value.detail == "bad token abcd-[token]"
    assert SECRET not in caplog.text
    fake.queue("/epics", _Resp(401, f"<html>{SECRET}</html>"))
    with pytest.raises(ShortcutAPIError) as excinfo:
        run_sync(fake, {})
    assert SECRET not in str(excinfo.value) and excinfo.value.detail == ""
    assert "InvalidHeader" in caplog.text  # the retries were logged by type


def test_waits_are_capped_and_ask_check_active_first(fake, sleeps):
    checks = []
    client = shortcut._Client(fake, lambda: checks.append(len(sleeps)))
    fake.queue("/members", _error(429, "Too Many Requests", headers={"Retry-After": "30"}))
    fake.queue("/members", _error(429, "Too Many Requests", headers={"Retry-After": "61"}))

    with pytest.raises(ShortcutAPIError) as excinfo:
        _request(client, "GET", "/members", list)
    assert excinfo.value.status == 429
    assert sleeps == [30.0]  # the 61 s wait was not taken
    assert checks == [0, 0, 1]  # before the request, before the wait, before the retry


def test_redact_cuts_the_token_out_and_drops_text_for_a_short_one():
    assert shortcut._redact(f"bad token {SECRET}", SECRET) == "bad token [token]"
    assert shortcut._redact("Resource not found.", SECRET) == "Resource not found."
    assert shortcut._redact("anything", "") == "anything"
    assert shortcut._redact("bad token abc", "abc") == ""  # too short to replace safely


def test_a_run_waits_at_most_its_budget_in_all(fake, sleeps):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for story_id in (1, 2):
        fake.stories[story_id]["updated_at"] = at(5)
    for _ in range(shortcut._MAX_RETRIES):
        fake.queue("/stories/1", _error(503, "Unavailable", headers={"Retry-After": "50"}))
    stats = {}

    rows = run_sync(fake, state, stats=stats)

    assert sleeps == [50.0, 50.0]  # a third wait would pass the 120 s budget
    assert stats["failed_unavailable"] == 1 and stats["failed"] == 1
    assert story_rows(rows) == {} and tombstones(rows) == []
    assert set(story_rows(run_sync(fake, state))) == {"story:1", "story:2"}  # resumed


@pytest.mark.parametrize("name", ["", "Foo-Bar", "a" * 65, None])
def test_resource_name_is_validated(fake, name):
    with pytest.raises(ValueError, match="resource_name"):
        shortcut_source(service=fake, resource_name=name)


def test_service_is_the_injection_kwarg_and_session_its_alias(fake):
    fake.add_story(1, updated_at=at(1))
    assert list(shortcut_source(service=fake, resource_name="alias_service"))
    assert list(shortcut_source(session=fake, resource_name="alias_session"))
    with pytest.raises(ValueError, match="not both"):
        shortcut_source(service=fake, session=fake)


def test_session_is_built_per_run_and_closed(fake, monkeypatch):
    fake.add_story(1, updated_at=at(1))
    built = []

    class Closing(FakeShortcut):
        closed = False

        def close(self):
            self.closed = True

    def make_session(token):
        session = Closing()
        session.__dict__.update(copy.deepcopy(fake.__dict__))
        built.append(session)
        return session

    monkeypatch.setattr(shortcut, "_make_session", make_session)
    source = shortcut_source(token="test-token", resource_name="closed_session")
    assert built == []  # nothing is opened before the run
    assert list(source)
    assert len(built) == 1 and built[0].closed


def test_sync_stats_count_what_a_run_did(fake):
    for story_id in (1, 2, 3):
        fake.add_story(story_id, updated_at=at(1))
    source = shortcut_source(service=fake, resource_name="stats_run")
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
    fake.queue("/epics", _error(429, "Too Many Requests", headers={"Retry-After": "120"}))
    list(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert source.cognee_sync_stats["failed_rate_limit"] == 1


# ---------------------------------------------------------------------------
# Review round 4: clocks, checks, re-asks, redaction
# ---------------------------------------------------------------------------
def _edited_and_hidden(fake, story_id=2):
    """Story ``story_id`` was edited and answers 403 on its next fetch."""
    fake.stories[story_id]["updated_at"] = at(5)
    fake.queue(f"/stories/{story_id}", _error(403, "Forbidden"))


def _clocks(fake, monkeypatch, shortcut_now, local_now):
    fake.clock = shortcut_now
    monkeypatch.setattr(shortcut, "_local_now", lambda: local_now)


def test_a_local_clock_jumping_ahead_forgets_nothing(fake, monkeypatch, caplog):
    # Three counted runs within three real minutes; on the third the local clock
    # is 25 hours ahead (an NTP step, a resumed VM).
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for minute, ahead in ((1, 0), (2, 0), (3, 25)):
        _edited_and_hidden(fake)
        moment = NOW + timedelta(minutes=minute)
        _clocks(fake, monkeypatch, moment, moment + timedelta(hours=ahead))
        stats = {}
        rows = list(sync_stories(fake, state, stats=stats))

    assert tombstones(rows) == [] and stats["given_up"] == 0 and state["given_up"] == {}
    assert state["unreadable"]["story:2"]["runs"] == 3
    assert "no item is forgotten in this run" in caplog.text


def test_the_give_up_times_are_read_on_shortcuts_clock(fake, monkeypatch):
    # The first counted run has a local clock 25 hours ahead: the 24 hours still
    # start at Shortcut's time, not the local one.
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    _edited_and_hidden(fake)
    _clocks(fake, monkeypatch, hours(1), hours(26))

    list(sync_stories(fake, state))

    assert state["unreadable"]["story:2"]["since"] == _stamp(hours(1))


def test_without_a_date_header_the_give_up_times_are_local(fake, monkeypatch):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    _edited_and_hidden(fake)
    _clocks(fake, monkeypatch, None, hours(3))

    list(sync_stories(fake, state))

    assert state["unreadable"]["story:2"]["since"] == _stamp(hours(3))


def test_nothing_is_forgotten_while_the_clocks_disagree(fake, monkeypatch):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    for run in (1, 2, 3):
        _edited_and_hidden(fake)
        run_sync(fake, state, now=hours(run))
    assert state["given_up"] == {}  # three runs, but within a day

    # Due by either clock, but they differ by two minutes: counted, not forgotten.
    _edited_and_hidden(fake)
    _clocks(fake, monkeypatch, hours(48), hours(48) + timedelta(minutes=2))
    rows = list(sync_stories(fake, state))
    assert tombstones(rows) == [] and state["unreadable"]["story:2"]["runs"] == 4

    # The clocks agree again: it is forgotten.
    _edited_and_hidden(fake)
    _clocks(fake, monkeypatch, hours(49), hours(49))
    rows = list(sync_stories(fake, state))
    assert [r["id"] for r in tombstones(rows)] == ["story:2"]


def test_a_reask_time_in_the_future_is_brought_back_to_now(fake, caplog):
    # A run with a clock 30 days ahead forgot story 2: it is asked a day after
    # the clock is right again, not 30 days later.
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    state["given_up"] = {"story:2": {"stamp": at(1), "at": _stamp(hours(24 * 30))}}
    state["known_ids"].remove("story:2")
    del state["fingerprints"]["2"]

    run_sync(fake, state, now=hours(1))
    assert state["given_up"]["story:2"]["at"] == _stamp(hours(1))
    assert "story:2 had at in the future" in caplog.text
    fake.calls.clear()
    run_sync(fake, state, now=hours(24))
    assert fake.fetched_stories() == []
    rows = run_sync(fake, state, now=hours(25))
    assert set(story_rows(rows)) == {"story:2"} and state["given_up"] == {}


def test_a_give_up_start_in_the_future_is_brought_back_to_now(fake):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    _edited_and_hidden(fake)
    run_sync(fake, state, now=hours(1))
    state["unreadable"]["story:2"] |= {"runs": 2, "since": _stamp(hours(24 * 30))}

    _edited_and_hidden(fake)
    run_sync(fake, state, now=hours(2))
    assert state["unreadable"]["story:2"] == {"stamp": at(5), "runs": 3, "since": _stamp(hours(2))}
    _edited_and_hidden(fake)
    rows = run_sync(fake, state, now=hours(25))
    assert tombstones(rows) == []  # 23 hours since the reset
    _edited_and_hidden(fake)
    rows = run_sync(fake, state, now=hours(26))
    assert [r["id"] for r in tombstones(rows)] == ["story:2"]


def test_a_hidden_check_story_moves_on_to_the_next_one(fake, caplog):
    # Story 2 was edited and is hidden; story 6, the newest unchanged one, is
    # hidden too; story 1 reads. Five runs over 48 hours.
    fake.add_story(1, updated_at=at(1))
    fake.add_story(2, updated_at=at(1))
    fake.add_story(6, updated_at=at(3))
    state = {}
    run_sync(fake, state)
    given_up = 0
    for run in range(5):
        _edited_and_hidden(fake)
        fake.queue("/stories/6", _error(403, "Forbidden"))
        fake.calls.clear()
        stats = {}
        run_sync(fake, state, now=hours(12 * run), stats=stats)
        assert "failed_permission" not in stats
        if run == 0:
            assert fake.fetched_stories() == [2, 6, 1] and stats["probe_story"] == 2
        given_up += stats["given_up"]
    assert given_up == 1 and "story:2" in state["given_up"]
    assert "check the token's permissions" not in caplog.text


def test_a_hidden_check_iteration_moves_on_to_the_next_one(fake):
    fake.add_iteration(3)
    fake.add_iteration(4, name="Sprint 13", updated_at=at(2))
    fake.add_iteration(5, name="Sprint 14")
    state = {}
    run_sync(fake, state)
    fake.iterations[5]["updated_at"] = at(5)
    for run in (1, 2, 3):
        fake.queue("/iterations/5", _error(403, "Forbidden"))
        fake.queue("/iterations/4", _error(403, "Forbidden"))
        stats = {}
        rows = run_sync(fake, state, now=hours(12 * run), stats=stats)
        assert stats["probe_iteration"] == 2 and "failed_permission" not in stats
    assert [r["id"] for r in tombstones(rows)] == ["iteration:5"]


def test_the_check_tries_three_items_at_most(fake, caplog):
    for story_id in (1, 2, 3, 4, 5):
        fake.add_story(story_id, updated_at=at(story_id))
    state = {}
    run_sync(fake, state)
    fake.stories[5]["updated_at"] = at(9)
    for story_id in (5, 4, 3, 2):
        fake.queue(f"/stories/{story_id}", _error(403, "Forbidden"))
    fake.calls.clear()
    stats = {}

    run_sync(fake, state, now=hours(12), stats=stats)

    assert fake.fetched_stories() == [5, 4, 3, 2]  # story 1 is never tried
    assert stats["probe_story"] == 3 and stats["failed_permission"] == 1
    assert state["unreadable"] == {}
    assert "check the token's permissions" in caplog.text


def test_a_check_answering_a_server_error_stops_the_checks(fake):
    for story_id in (1, 2, 3, 4):
        fake.add_story(story_id, updated_at=at(story_id))
    state = {}
    run_sync(fake, state)
    fake.stories[4]["updated_at"] = at(9)
    fake.queue("/stories/4", _error(403, "Forbidden"))
    fake.queue("/stories/3", _error(403, "Forbidden"))
    _fail(fake, "/stories/2")
    fake.calls.clear()
    stats = {}

    run_sync(fake, state, now=hours(12), stats=stats)

    assert 1 not in fake.fetched_stories() and stats["probe_story"] == 2
    assert "failed_permission" not in stats and state["unreadable"] == {}


def test_a_failed_reask_is_no_failure_of_the_run(fake, caplog):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    state["given_up"] = {"story:2": {"stamp": at(1), "at": _stamp(hours(-24))}}
    state["known_ids"].remove("story:2")
    del state["fingerprints"]["2"]
    for day in (0, 1, 2):
        fake.queue("/stories/2", _error(404, "Resource not found."))
        stats = {}
        rows = run_sync(fake, state, now=hours(24 * day), stats=stats)
        assert story_rows(rows) == {}
        assert stats["failed"] == 0 and stats["reask_failed"] == 1
    assert "story:2 is still unreadable" in caplog.text


def test_a_rate_limit_on_a_reask_still_stops_the_run(fake):
    for story_id in (1, 2):
        fake.add_story(story_id, updated_at=at(1))
    state = {}
    run_sync(fake, state)
    state["given_up"] = {"story:2": {"stamp": at(1), "at": _stamp(hours(-24))}}
    fake.queue("/stories/2", _error(429, "Too Many Requests", headers={"Retry-After": "61"}))
    stats = {}

    run_sync(fake, state, stats=stats)

    assert stats["failed_rate_limit"] == 1 and stats["failed"] == 0


def test_the_token_is_cut_out_before_the_detail_is_shortened(fake):
    fake.headers = {"Shortcut-Token": SECRET}
    fake.queue("/epics", _Resp(401, {"message": "x" * 195 + SECRET}))
    with pytest.raises(ShortcutAPIError) as excinfo:
        run_sync(fake, {})
    assert SECRET[:5] not in excinfo.value.detail
    assert excinfo.value.detail == "x" * 195 + "[toke"


def test_redact_ignores_case():
    assert shortcut._redact(f"bad token {SECRET.lower()}", SECRET) == "bad token [token]"
    assert shortcut._redact("bad token ABC", "abc") == ""
