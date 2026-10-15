"""Test suite for the ClickUp data-source connector (fully mocked, no network)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_clickup import (
    CLICKUP_DOCS_TABLE_NAME,
    CLICKUP_SOURCE_NAME,
    CLICKUP_TABLE_NAME,
    clickup_source,
    sync_clickup,
    sync_clickup_docs,
)
from cognee_community_connector_clickup import clickup as clickup_module
from cognee_community_connector_clickup.clickup import (
    _ClickUpAPI,
    _extract_retry_delay,
    _fetch_task_comments,
    _format_custom_field,
    _format_timestamp,
    _make_session,
    _render_task_markdown,
    _request,
    _resolve_team_id,
    resolve_hierarchy_cache,
)

TEAM = "9001"
BASE = "https://api.clickup.com/api"


# ---------------------------------------------------------------------------
# Fake ClickUp API
# ---------------------------------------------------------------------------


def make_task(
    task_id: str,
    updated: int,
    *,
    name: str | None = None,
    space: str = "s1",
    folder: dict[str, Any] | None = None,
    list_: dict[str, Any] | None = None,
    closed: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    task = {
        "id": task_id,
        "name": name or f"Task {task_id}",
        "status": {
            "status": "complete" if closed else "in progress",
            "type": "closed" if closed else "custom",
        },
        "priority": {"priority": "high"},
        "assignees": [{"id": 1, "username": "Bob"}],
        "creator": {"id": 2, "username": "Alice"},
        "tags": [],
        "date_created": "1690000000000",
        "date_updated": str(updated),
        "markdown_description": f"Description of **{task_id}**.",
        "text_content": f"Description of {task_id}.",
        "space": {"id": space},
        "folder": folder or {"id": "f1", "name": "Backend", "hidden": False},
        "list": list_ or {"id": "l1", "name": "Sprint 42"},
        "url": f"https://app.clickup.com/t/{task_id}",
        "custom_fields": [],
        "checklists": [],
    }
    task.update(extra)
    return task


def make_doc(doc_id: str, updated: int, *, parent: str = "s1", **extra: Any) -> dict[str, Any]:
    doc = {
        "id": doc_id,
        "name": f"Doc {doc_id}",
        "date_created": 1690000000000,
        "date_updated": updated,
        "deleted": False,
        "archived": False,
        "parent": {"id": parent, "type": 4},
        "workspace_id": int(TEAM),
    }
    doc.update(extra)
    return doc


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeClickUpSession:
    """In-memory ClickUp API v2 (tasks) + v3 (docs) with the real paging rules."""

    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        self.teams = [{"id": TEAM, "name": "Acme"}]
        self.spaces = [{"id": "s1", "name": "Engineering"}, {"id": "s2", "name": "Marketing"}]
        self.tasks: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks or []}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.scripted: list[FakeResponse | Exception] = []
        self.empty_full_listing = False  # simulate a glitchy full (un-cursored) listing

    def add(self, *tasks: dict[str, Any]) -> None:
        for task in tasks:
            self.tasks[task["id"]] = task

    def add_doc(self, doc: dict[str, Any], pages: list[dict[str, Any]]) -> None:
        self.docs[doc["id"]] = doc
        self.pages[doc["id"]] = pages

    def request(self, method: str, url: str, params: dict | None = None, timeout=None):
        params = dict(params or {})
        path = url.removeprefix(BASE)
        self.calls.append((path, params))
        if self.scripted:
            scripted = self.scripted.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted

        if path == "/v2/team":
            return FakeResponse(200, {"teams": self.teams})
        if path == f"/v2/team/{TEAM}/space":
            return FakeResponse(200, {"spaces": self.spaces})
        if path == f"/v2/team/{TEAM}/task":
            return self._tasks(params)
        if path.startswith("/v2/task/") and path.endswith("/comment"):
            return self._comments(path.split("/")[3], params)
        if path == f"/v3/workspaces/{TEAM}/docs":
            return self._docs(params)
        if path.startswith(f"/v3/workspaces/{TEAM}/docs/") and path.endswith("/pages"):
            doc_id = path.split("/")[5]
            if doc_id not in self.docs:
                return FakeResponse(404, {"err": "Doc not found"})
            return FakeResponse(200, self.pages[doc_id])
        return FakeResponse(404, {"err": "Route not found"})

    def _tasks(self, params: dict[str, Any]) -> FakeResponse:
        rows = sorted(self.tasks.values(), key=lambda t: int(t["date_updated"]), reverse=True)
        if "date_updated_gt" in params:
            # Despite the name, the live API returns tasks updated *at* the cursor too.
            rows = [t for t in rows if int(t["date_updated"]) >= int(params["date_updated_gt"])]
        elif self.empty_full_listing:
            rows = []
        if params.get("include_closed") != "true":
            rows = [t for t in rows if t["status"]["type"] != "closed"]
        for key, field in (
            ("space_ids[]", "space"),
            ("project_ids[]", "folder"),
            ("list_ids[]", "list"),
        ):
            if key in params:
                rows = [t for t in rows if t[field]["id"] in params[key]]
        page = int(params.get("page", 0))
        chunk = rows[page * 100 : (page + 1) * 100]
        return FakeResponse(200, {"tasks": chunk, "last_page": (page + 1) * 100 >= len(rows)})

    def _comments(self, task_id: str, params: dict[str, Any]) -> FakeResponse:
        if task_id not in self.tasks:
            return FakeResponse(404, {"err": "Task not found"})
        newest_first = sorted(
            self.comments.get(task_id, []), key=lambda c: int(c["date"]), reverse=True
        )
        if "start" in params:
            newest_first = [c for c in newest_first if int(c["date"]) < int(params["start"])]
        return FakeResponse(200, {"comments": newest_first[:25]})

    def _docs(self, params: dict[str, Any]) -> FakeResponse:
        docs = sorted(self.docs.values(), key=lambda d: d["id"])
        offset = int(params.get("cursor") or 0)
        limit = int(params["limit"])
        page = docs[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(docs) else None
        return FakeResponse(200, {"docs": page, "next_cursor": next_cursor})

    def paths(self, prefix: str = "") -> list[str]:
        return [p for p, _ in self.calls if p.startswith(prefix)]

    def task_listings(self) -> list[dict[str, Any]]:
        return [params for path, params in self.calls if path == f"/v2/team/{TEAM}/task"]


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("time.sleep") as sleep:
        yield sleep


def api_for(session: FakeClickUpSession) -> _ClickUpAPI:
    return _ClickUpAPI(session)


def run_tasks(session, state, cache=None, **kwargs) -> list[dict[str, Any]]:
    cache = cache if cache is not None else {}
    return list(sync_clickup(api_for(session), state, cache, team_id=TEAM, **kwargs))


def run_docs(session, state, cache=None, **kwargs) -> list[dict[str, Any]]:
    cache = cache if cache is not None else {}
    return list(sync_clickup_docs(api_for(session), state, cache, team_id=TEAM, **kwargs))


def ids(rows, *, deleted: bool | None = None) -> list[str]:
    return [r["id"] for r in rows if deleted is None or bool(r.get("_deleted")) is deleted]


# ---------------------------------------------------------------------------
# Auth & workspace selection
# ---------------------------------------------------------------------------


def test_personal_token_is_sent_raw_in_the_authorization_header():
    session = _make_session("  pk_123_ABC  ")
    assert session.headers["Authorization"] == "pk_123_ABC"


def test_source_reads_the_token_from_the_environment(monkeypatch):
    seen = {}
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_env")
    monkeypatch.setattr(clickup_module, "_make_session", lambda token: seen.setdefault("t", token))
    clickup_source()
    assert seen["t"] == "pk_env"


def test_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CLICKUP_API_TOKEN"):
        clickup_source()


def test_building_the_source_makes_no_requests():
    session = FakeClickUpSession()
    clickup_source(session=session)
    assert session.calls == []


def test_single_workspace_is_picked_automatically_and_several_must_be_chosen():
    session = FakeClickUpSession()
    assert _resolve_team_id(api_for(session)) == TEAM

    session.teams.append({"id": "42", "name": "Side project"})
    with pytest.raises(ValueError, match="pass team_id="):
        _resolve_team_id(api_for(session))
    assert _resolve_team_id(api_for(session), "42") == "42"


def test_invalid_token_raises_permission_error():
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(401, {"err": "Token invalid"})]
    with pytest.raises(PermissionError, match="CLICKUP_API_TOKEN"):
        run_tasks(session, {})


# ---------------------------------------------------------------------------
# Rate limits & retries
# ---------------------------------------------------------------------------


def test_retry_delay_prefers_retry_after_then_rate_limit_reset():
    assert _extract_retry_delay({"Retry-After": "7"}, 0) == 7.0
    with patch("time.time", return_value=1_000.0):
        assert _extract_retry_delay({"X-RateLimit-Reset": "1012"}, 0) == 12.0
        assert _extract_retry_delay({"X-RateLimit-Reset": "999999"}, 0) == 60.0
    assert _extract_retry_delay({}, 3) == 8.0


def test_429_and_5xx_are_retried(_no_sleep):
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(503)]

    body = _request(session, f"{BASE}/v2/team")

    assert body["teams"][0]["id"] == TEAM
    assert [c.args[0] for c in _no_sleep.call_args_list] == [3.0, 2.0]


def test_dropped_connections_are_retried_and_give_up_eventually():
    import requests

    session = FakeClickUpSession()
    session.scripted = [requests.exceptions.ConnectionError("reset")]
    assert _request(session, f"{BASE}/v2/team")["teams"]

    session.scripted = [FakeResponse(502)] * 10
    with pytest.raises(requests.exceptions.HTTPError):
        _request(session, f"{BASE}/v2/team")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_task_markdown_includes_every_field_without_a_title_heading():
    task = make_task(
        "t1",
        1_700_000_000_000,
        tags=[{"name": "backend"}, {"name": "urgent"}],
        parent="t0",
        due_date="1700086400000",
        checklists=[
            {"name": "QA", "items": [{"name": "Unit tests", "resolved": True}, {"name": "Docs"}]}
        ],
    )
    comments = [{"user": "Carol", "date": "2023-11-14 22:13:20 UTC", "text": "Looks good"}]

    md = _render_task_markdown(task, "Engineering / Backend / Sprint 42", comments)

    assert md.startswith("**Status:** in progress | **Priority:** high | **Assignees:** Bob\n")
    assert "**Tags:** backend, urgent" in md
    assert "**Location:** Engineering / Backend / Sprint 42" in md
    assert "**Subtask of:** t0" in md
    assert "**Creator:** Alice" in md
    assert "**Due:** 2023-11-15 22:13:20 UTC" in md
    assert "## Description\nDescription of **t1**." in md  # markdown preferred over plain text
    assert "### QA\n- [x] Unit tests\n- [ ] Docs" in md
    assert "## Comments\n- **Carol** (2023-11-14 22:13:20 UTC): Looks good" in md
    assert not md.startswith("#")


def test_format_timestamp_is_utc_and_tolerant():
    assert _format_timestamp("1700000000000") == "2023-11-14 22:13:20 UTC"
    assert _format_timestamp(None) == ""
    assert _format_timestamp("soon") == "soon"


_OPTIONS = {
    "options": [
        {"id": "a", "name": "Low", "orderindex": 0},
        {"id": "b", "name": "High", "orderindex": 1},
    ]
}


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ({"type": "drop_down", "value": 1, "type_config": _OPTIONS}, "High"),
        ({"type": "drop_down", "value": "a", "type_config": _OPTIONS}, "Low"),
        (
            {
                "type": "labels",
                "value": ["x", "y"],
                "type_config": {
                    "options": [{"id": "x", "label": "API"}, {"id": "y", "label": "UI"}]
                },
            },
            "API, UI",
        ),
        (
            {
                "type": "users",
                "value": [{"id": 1, "username": "Bob"}, {"id": 2, "email": "c@x.io"}],
            },
            "Bob, c@x.io",
        ),
        ({"type": "tasks", "value": [{"id": "t9", "name": "Blocker"}]}, "Blocker"),
        ({"type": "date", "value": "1700006400000"}, "2023-11-15"),
        ({"type": "checkbox", "value": "true"}, "Yes"),
        ({"type": "currency", "value": 1200, "type_config": {"currency_type": "USD"}}, "1200 USD"),
        ({"type": "emoji", "value": 4, "type_config": {"count": 5}}, "4/5"),
        (
            {"type": "location", "value": {"formatted_address": "Berlin, Germany"}},
            "Berlin, Germany",
        ),
        ({"type": "automatic_progress", "value": {"percent_completed": 40}}, "40%"),
        ({"type": "short_text", "value": " plain "}, "plain"),
        ({"type": "short_text", "value": None}, ""),
    ],
)
def test_custom_fields_are_decoded_by_type(field, expected):
    assert _format_custom_field({"name": "F", **field}) == expected


def test_breadcrumb_skips_the_hidden_folder_of_folderless_lists():
    hidden = {"id": "h", "name": "hidden", "hidden": True}
    session = FakeClickUpSession(
        [make_task("t1", 1, folder=hidden, list_={"id": "l9", "name": "Ideas"})]
    )
    (row,) = run_tasks(session, {})
    assert "**Location:** Engineering / Ideas" in row["content"]


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def test_all_comments_are_fetched_across_pages_oldest_first():
    session = FakeClickUpSession([make_task("t1", 1)])
    session.comments["t1"] = [
        {
            "id": str(i),
            "comment_text": f"c{i}",
            "user": {"username": "U"},
            "date": str(1_700_000_000_000 + i),
        }
        for i in range(60)
    ]

    comments = _fetch_task_comments(api_for(session), "t1")

    assert [c["text"] for c in comments] == [f"c{i}" for i in range(60)]
    assert len(session.paths("/v2/task/t1/comment")) == 3


def test_comments_of_a_task_deleted_mid_sync_are_skipped():
    assert _fetch_task_comments(api_for(FakeClickUpSession()), "gone") == []


# ---------------------------------------------------------------------------
# Hierarchy cache
# ---------------------------------------------------------------------------


def test_hierarchy_is_cached_and_never_rewalked_on_later_syncs():
    session = FakeClickUpSession([make_task("t1", 1)])
    state: dict[str, Any] = {}
    cache: dict[str, Any] = {}

    run_tasks(session, state, cache)
    session.add(make_task("t2", 2))
    run_tasks(session, state, cache)

    assert session.paths(f"/v2/team/{TEAM}/space") == [f"/v2/team/{TEAM}/space"]  # one walk
    assert not session.paths("/v2/space/") and not session.paths("/v2/folder/")
    assert cache["spaces"] == {"s1": "Engineering", "s2": "Marketing"}


def test_unknown_space_triggers_a_single_refresh():
    session = FakeClickUpSession([make_task("t1", 1)])
    cache: dict[str, Any] = {}
    state: dict[str, Any] = {}
    run_tasks(session, state, cache)

    session.spaces.append({"id": "s3", "name": "Research"})
    session.add(make_task("t2", 2, space="s3"), make_task("t3", 3, space="s3"))
    rows = run_tasks(session, state, cache)

    assert len(session.paths(f"/v2/team/{TEAM}/space")) == 2
    assert all("**Location:** Research /" in r["content"] for r in rows)


def test_hierarchy_cache_is_per_workspace():
    session = FakeClickUpSession()
    cache = {"team_id": "other", "spaces": {"x": "Stale"}}
    assert resolve_hierarchy_cache(api_for(session), TEAM, cache) == {
        "s1": "Engineering",
        "s2": "Marketing",
    }


# ---------------------------------------------------------------------------
# Incremental task sync
# ---------------------------------------------------------------------------


def test_initial_backfill_pages_through_every_task():
    session = FakeClickUpSession([make_task(f"t{i}", 1_000 + i) for i in range(230)])
    state: dict[str, Any] = {}

    rows = run_tasks(session, state, include_comments=False)

    assert len(rows) == 230
    assert [p["page"] for p in session.task_listings()] == [0, 1, 2]
    assert all(p["include_markdown_description"] == "true" for p in session.task_listings())
    assert "date_updated_gt" not in session.task_listings()[0]
    assert state["last_updated_ms"] == 1_229
    assert len(state["known_ids"]) == 230


def test_rows_carry_document_fields():
    session = FakeClickUpSession([make_task("t1", 5, name="Ship v2")])
    (row,) = run_tasks(session, {})
    assert row["id"] == "clickup:task:t1"
    assert row["title"] == "Ship v2"
    assert row["url"] == "https://app.clickup.com/t/t1"
    assert (row["status"], row["list"], row["date_updated"], row["_deleted"]) == (
        "in progress",
        "Sprint 42",
        5,
        False,
    )


def test_incremental_sync_uses_date_updated_gt_and_yields_only_changes():
    session = FakeClickUpSession([make_task("t1", 100), make_task("t2", 200)])
    state: dict[str, Any] = {}
    run_tasks(session, state)
    session.calls.clear()

    session.add(make_task("t1", 300, name="Task t1 (edited)"), make_task("t3", 250))
    rows = run_tasks(session, state)

    assert ids(rows) == ["clickup:task:t1", "clickup:task:t3"]
    assert session.task_listings()[0]["date_updated_gt"] == "200"
    assert sorted(session.paths("/v2/task/")) == ["/v2/task/t1/comment", "/v2/task/t3/comment"]
    assert state["last_updated_ms"] == 300


def test_no_op_rerun_yields_nothing_and_keeps_state():
    session = FakeClickUpSession([make_task("t1", 100)])
    state: dict[str, Any] = {}
    run_tasks(session, state)
    snapshot = json.dumps(state, sort_keys=True)
    session.calls.clear()

    assert run_tasks(session, state) == []
    assert not session.paths("/v2/task/")  # no comment fetches
    assert json.dumps(state, sort_keys=True) == snapshot


def test_new_task_sharing_the_cursor_millisecond_is_not_skipped():
    session = FakeClickUpSession([make_task("t1", 500)])
    state: dict[str, Any] = {}
    run_tasks(session, state)

    session.add(make_task("t2", 500))  # same date_updated as the task that set the cursor
    assert ids(run_tasks(session, state)) == ["clickup:task:t2"]
    assert run_tasks(session, state) == []


def test_scope_filters_are_sent_and_changing_scope_resets_the_cursor():
    session = FakeClickUpSession(
        [make_task("eng", 100, space="s1"), make_task("mkt", 200, space="s2")]
    )
    state: dict[str, Any] = {}

    rows = run_tasks(session, state, space_ids=["s1"])
    assert ids(rows) == ["clickup:task:eng"]
    assert session.task_listings()[0]["space_ids[]"] == ["s1"]

    rows = run_tasks(session, state, space_ids=["s2"])
    assert ids(rows, deleted=False) == ["clickup:task:mkt"]
    assert ids(rows, deleted=True) == ["clickup:task:eng"]  # no longer selected


def test_closed_tasks_can_be_excluded():
    session = FakeClickUpSession([make_task("open", 1), make_task("done", 2, closed=True)])
    assert ids(run_tasks(session, {}, include_closed=False)) == ["clickup:task:open"]


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------


def test_deleted_task_becomes_a_tombstone():
    session = FakeClickUpSession([make_task("t1", 100), make_task("t2", 200)])
    state: dict[str, Any] = {}
    run_tasks(session, state)

    del session.tasks["t1"]
    rows = run_tasks(session, state)

    assert rows == [{"id": "clickup:task:t1", "_deleted": True}]
    assert state["known_ids"] == ["t2"]


def test_empty_listing_skips_the_sweep_but_keeps_newly_emitted_ids():
    session = FakeClickUpSession([make_task("t1", 100)])
    state: dict[str, Any] = {}
    run_tasks(session, state)

    session.add(make_task("t2", 200))
    session.empty_full_listing = True  # the un-cursored sweep glitches and returns nothing
    rows = run_tasks(session, state)

    assert ids(rows) == ["clickup:task:t2"]
    assert state["known_ids"] == ["t1", "t2"]  # t2 is tracked, so a later delete is noticed

    session.empty_full_listing = False
    del session.tasks["t2"]
    assert run_tasks(session, state) == [{"id": "clickup:task:t2", "_deleted": True}]


def test_failure_mid_sync_leaves_state_untouched():
    session = FakeClickUpSession([make_task("t1", 100)])
    state: dict[str, Any] = {}
    run_tasks(session, state)
    before = json.dumps(state, sort_keys=True)

    session.add(make_task("t2", 200))
    del session.tasks["t1"]
    original = session._comments
    session._comments = lambda task_id, params: FakeResponse(400, {"err": "bad"})  # type: ignore[method-assign]
    with pytest.raises(Exception, match="400"):
        run_tasks(session, state)
    assert json.dumps(state, sort_keys=True) == before

    session._comments = original  # type: ignore[method-assign]
    rows = run_tasks(session, state)
    assert ids(rows) == ["clickup:task:t2", "clickup:task:t1"]
    assert rows[1]["_deleted"] is True


def test_detect_deletions_off_skips_the_sweep():
    session = FakeClickUpSession([make_task("t1", 100)])
    state: dict[str, Any] = {}
    run_tasks(session, state)
    session.calls.clear()

    del session.tasks["t1"]
    assert run_tasks(session, state, detect_deletions=False) == []
    assert len(session.task_listings()) == 1


HOUR_MS = 3_600_000


def _comment(i: int, text: str) -> dict[str, Any]:
    return {"id": str(i), "comment_text": text, "user": {"username": "U"}, "date": str(1_000 + i)}


def test_deleted_comment_is_dropped_on_the_next_comment_refresh():
    """Verified live: deleting a comment does not change the task's date_updated."""
    session = FakeClickUpSession([make_task("t1", 100), make_task("t2", 200)])
    session.comments["t1"] = [_comment(1, "keep me"), _comment(2, "delete me")]
    state: dict[str, Any] = {}
    run_tasks(session, state, now_ms=0)

    session.comments["t1"] = [_comment(1, "keep me")]  # date_updated stays 100
    assert run_tasks(session, state, now_ms=1 * HOUR_MS) == []  # refresh not due yet

    rows = run_tasks(session, state, now_ms=25 * HOUR_MS)
    assert ids(rows) == ["clickup:task:t1"]
    assert "keep me" in rows[0]["content"]
    assert "delete me" not in rows[0]["content"]
    assert state["comments_refreshed_ms"] == 25 * HOUR_MS


def test_edited_comment_is_picked_up_by_the_refresh():
    session = FakeClickUpSession([make_task("t1", 100)])
    session.comments["t1"] = [_comment(1, "first draft")]
    state: dict[str, Any] = {}
    run_tasks(session, state, now_ms=0)

    session.comments["t1"] = [_comment(1, "final wording")]
    (row,) = run_tasks(session, state, now_ms=24 * HOUR_MS)
    assert "final wording" in row["content"]


def test_refresh_with_unchanged_comments_yields_nothing():
    session = FakeClickUpSession([make_task("t1", 100), make_task("t2", 200)])
    session.comments["t1"] = [_comment(1, "same")]
    state: dict[str, Any] = {}
    run_tasks(session, state, now_ms=0)
    session.calls.clear()

    assert run_tasks(session, state, now_ms=30 * HOUR_MS) == []
    assert sorted(session.paths("/v2/task/")) == ["/v2/task/t1/comment", "/v2/task/t2/comment"]
    assert len(session.task_listings()) == 2  # incremental + one shared full listing


def test_comment_refresh_can_be_disabled():
    session = FakeClickUpSession([make_task("t1", 100)])
    session.comments["t1"] = [_comment(1, "gone soon")]
    state: dict[str, Any] = {}
    run_tasks(session, state, now_ms=0, comment_refresh_hours=None)

    session.comments["t1"] = []
    session.calls.clear()
    assert run_tasks(session, state, now_ms=100 * HOUR_MS, comment_refresh_hours=None) == []
    assert not session.paths("/v2/task/")


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


def _doc_session() -> FakeClickUpSession:
    session = FakeClickUpSession()
    session.add_doc(
        make_doc("d1", 1_000),
        [
            {
                "id": "p1",
                "name": "Architecture",
                "content": "We use **dlt**.",
                "pages": [{"id": "p2", "name": "Storage", "content": "SQLite staging."}],
            },
            {"id": "p3", "name": "Old page", "content": "x", "deleted": True},
        ],
    )
    session.add_doc(
        make_doc("d2", 2_000, parent="s2"), [{"id": "p4", "name": "Launch", "content": "Q4"}]
    )
    return session


def test_docs_are_rendered_with_nested_pages():
    session = _doc_session()
    state: dict[str, Any] = {}

    rows = run_docs(session, state)

    assert ids(rows) == ["clickup:doc:d1", "clickup:doc:d2"]
    doc = rows[0]
    assert doc["title"] == "Doc d1"
    assert doc["url"] == f"https://app.clickup.com/{TEAM}/v/dc/d1"
    assert "**Location:** Engineering" in doc["content"]
    assert "## Architecture\nWe use **dlt**." in doc["content"]
    assert "### Storage\nSQLite staging." in doc["content"]
    assert "Old page" not in doc["content"]
    pages_params = next(p for path, p in session.calls if path.endswith("/pages"))
    assert pages_params == {"max_page_depth": -1, "content_format": "text/md"}
    assert state["versions"] == {"d1": 1_000, "d2": 2_000}


def test_unchanged_docs_are_not_refetched_and_edited_ones_are():
    session = _doc_session()
    state: dict[str, Any] = {}
    run_docs(session, state)
    session.calls.clear()

    assert run_docs(session, state) == []
    assert not [p for p, _ in session.calls if p.endswith("/pages")]

    session.docs["d2"]["date_updated"] = 3_000
    session.pages["d2"] = [{"id": "p4", "name": "Launch", "content": "Moved to Q1"}]
    rows = run_docs(session, state)
    assert ids(rows) == ["clickup:doc:d2"]
    assert "Moved to Q1" in rows[0]["content"]


def test_deleted_and_archived_docs_become_tombstones():
    session = _doc_session()
    session.add_doc(make_doc("d3", 3_000), [])  # stays, so the listing is not empty
    state: dict[str, Any] = {}
    run_docs(session, state)

    del session.docs["d1"]
    session.docs["d2"]["archived"] = True
    rows = run_docs(session, state)

    assert rows == [
        {"id": "clickup:doc:d1", "_deleted": True},
        {"id": "clickup:doc:d2", "_deleted": True},
    ]


def test_docs_are_scoped_to_the_selected_containers():
    session = _doc_session()
    assert ids(run_docs(session, {}, container_ids=["s2"])) == ["clickup:doc:d2"]


def test_docs_listing_pages_through_the_cursor():
    session = FakeClickUpSession()
    for i in range(130):
        session.add_doc(make_doc(f"d{i:03d}", 1), [])
    rows = run_docs(session, {})
    assert len(rows) == 130
    assert len(session.paths(f"/v3/workspaces/{TEAM}/docs")) == 2 + 130  # 2 listings + pages


def test_empty_doc_listing_skips_the_sweep():
    session = _doc_session()
    state: dict[str, Any] = {}
    run_docs(session, state)

    session.docs.clear()
    assert run_docs(session, state) == []
    assert state["versions"] == {"d1": 1_000, "d2": 2_000}


# ---------------------------------------------------------------------------
# Source & end-to-end pipeline
# ---------------------------------------------------------------------------


def test_source_declares_document_marker_and_tables():
    source = clickup_source(session=FakeClickUpSession())
    assert CLICKUP_SOURCE_NAME == "clickup"
    assert document_source_tag(source) == "clickup"
    assert sorted(source.resources) == [CLICKUP_DOCS_TABLE_NAME, CLICKUP_TABLE_NAME]

    tasks_only = clickup_source(session=FakeClickUpSession(), include_docs=False)
    assert list(tasks_only.resources) == [CLICKUP_TABLE_NAME]


def _staged(pipeline, table: str) -> dict[str, str]:
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_dlt_pipeline_merge_purges_deleted_tasks_and_docs(tmp_path):
    dlt = pytest.importorskip("dlt")
    session = _doc_session()
    session.add(make_task("t1", 100, name="Alpha"), make_task("t2", 200, name="Beta"))
    pipeline = dlt.pipeline(
        pipeline_name="clickup_test_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_staging.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state"),
    )

    pipeline.run(clickup_source(session=session))
    assert _staged(pipeline, CLICKUP_TABLE_NAME) == {
        "clickup:task:t1": "Alpha",
        "clickup:task:t2": "Beta",
    }
    assert set(_staged(pipeline, CLICKUP_DOCS_TABLE_NAME)) == {"clickup:doc:d1", "clickup:doc:d2"}

    # Upstream: t1 and d1 are deleted, t3 is created.
    del session.tasks["t1"]
    del session.docs["d1"]
    session.add(make_task("t3", 300, name="Gamma"))
    session.calls.clear()
    pipeline.run(clickup_source(session=session))

    assert _staged(pipeline, CLICKUP_TABLE_NAME) == {
        "clickup:task:t2": "Beta",
        "clickup:task:t3": "Gamma",
    }
    assert set(_staged(pipeline, CLICKUP_DOCS_TABLE_NAME)) == {"clickup:doc:d2"}
    # Cursor and hierarchy cache both survived in dlt state.
    assert session.task_listings()[0]["date_updated_gt"] == "200"
    assert not session.paths(f"/v2/team/{TEAM}/space")
