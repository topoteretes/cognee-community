"""Test suite for the ClickUp data-source connector (fully mocked, no network)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

try:
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR
except ImportError:
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

from cognee_community_connector_clickup import (
    CLICKUP_DOCS_TABLE_NAME,
    CLICKUP_SOURCE_NAME,
    CLICKUP_TABLE_NAME,
    ClickUpAuthenticationError,
    ClickUpError,
    ClickUpNotFoundError,
    ClickUpPermissionError,
    ClickUpRateLimitError,
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
        # simulate a glitchy full (un-cursored) listing
        self.empty_full_listing = False
        self._doc_pages: Callable[[str, dict[str, Any]], FakeResponse] | None = None
        self.forbidden_tasks: set[str] = set()  # GET /task/{id} answers 403 for these

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
        if path.startswith("/v2/task/") and not path.endswith("/comment"):
            task_id = path.split("/")[3]
            if task_id in self.forbidden_tasks:
                return FakeResponse(403, {"err": "Task restricted"})
            if task_id in self.tasks:
                return FakeResponse(200, self.tasks[task_id])
            return FakeResponse(404, {"err": "Task not found"})
        if path == f"/v3/workspaces/{TEAM}/docs":
            return self._docs(params)
        if path.startswith(f"/v3/workspaces/{TEAM}/docs/") and path.endswith("/pages"):
            doc_id = path.split("/")[5]
            if self._doc_pages is not None:
                resp = self._doc_pages(doc_id, params)
                if resp is not None:
                    return resp
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

        def _to_date(c: dict[str, Any]) -> float:
            try:
                return float(c.get("date") or 0)
            except (ValueError, TypeError):
                return 0.0

        newest_first = sorted(self.comments.get(task_id, []), key=_to_date, reverse=True)
        if "start" in params:
            try:
                start_val = float(params["start"])
                newest_first = [c for c in newest_first if _to_date(c) < start_val]
            except (ValueError, TypeError):
                pass
        return FakeResponse(200, {"comments": newest_first[:25]})

    def _docs(self, params: dict[str, Any]) -> FakeResponse:
        docs = sorted(self.docs.values(), key=lambda d: d["id"])
        try:
            offset = int(params.get("cursor") or 0)
        except (ValueError, TypeError):
            offset = 0
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


def test_source_passes_explicit_api_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(clickup_module, "_make_session", lambda token: seen.setdefault("t", token))
    clickup_source(api_token="pk_explicit_123")
    assert seen["t"] == "pk_explicit_123"


def test_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="pass api_token="):
        clickup_source()


def test_building_the_source_makes_no_requests():
    session = FakeClickUpSession()
    clickup_source(session=session)
    assert session.calls == []


def test_single_workspace_is_picked_automatically_and_several_must_be_chosen():
    session = FakeClickUpSession()
    assert _resolve_team_id(api_for(session)) == TEAM

    session.teams.append({"id": "42", "name": "Side project"})
    with pytest.raises(ClickUpError, match="pass team_id="):
        _resolve_team_id(api_for(session))
    assert _resolve_team_id(api_for(session), "42") == "42"


def test_invalid_token_raises_authentication_error():
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(401, {"err": "Token invalid"})]
    with pytest.raises(ClickUpAuthenticationError) as exc_info:
        run_tasks(session, {})
    assert exc_info.value.status_code == 401
    assert isinstance(exc_info.value, PermissionError)


def test_401_error_sanitizes_token():
    """Assert the secret token and URL do NOT appear in the exception message on 401."""
    secret_token = "pk_super_secret_token_12345"
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(401, {"err": "Unauthorized"})]

    with pytest.raises(ClickUpAuthenticationError) as exc_info:
        api = _ClickUpAPI(session)
        api.v2("/team")

    msg = str(exc_info.value)
    assert secret_token not in msg
    assert "/v2/team" not in msg
    assert "api.clickup.com" not in msg
    assert exc_info.value.status_code == 401


def test_check_active_aborts_request():
    session = FakeClickUpSession()

    def abort():
        raise RuntimeError("Operation aborted by check_active")

    api = _ClickUpAPI(session, check_active=abort)
    with pytest.raises(RuntimeError, match="Operation aborted by check_active"):
        api.v2("/team")


def test_403_forbidden_raises_permission_error():
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(403, {"err": "Forbidden"})]
    with pytest.raises(ClickUpPermissionError) as exc_info:
        api = _ClickUpAPI(session)
        api.v2("/team")
    assert exc_info.value.status_code == 403
    assert isinstance(exc_info.value, PermissionError)


def test_404_not_found_raises_not_found_error():
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(404, {"err": "Not found"})]
    with pytest.raises(ClickUpNotFoundError) as exc_info:
        api = _ClickUpAPI(session)
        api.v2("/team")
    assert exc_info.value.status_code == 404


def test_429_exhausted_retries_raises_rate_limit_error():
    session = FakeClickUpSession()
    session.scripted = [FakeResponse(429, headers={"Retry-After": "1"})] * 5
    with pytest.raises(ClickUpRateLimitError) as exc_info:
        api = _ClickUpAPI(session)
        api.v2("/team")
    assert exc_info.value.status_code == 429


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
    with pytest.raises((ClickUpError, requests.exceptions.HTTPError)):
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
    # markdown preferred over plain text
    assert "## Description\nDescription of **t1**." in md
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
    assert row["_deleted"] is False
    assert "status" not in row
    assert "list" not in row
    assert "date_updated" not in row


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

    # same date_updated as the task that set the cursor
    session.add(make_task("t2", 500))
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
    # the un-cursored sweep glitches and returns nothing
    session.empty_full_listing = True
    rows = run_tasks(session, state)

    assert ids(rows) == ["clickup:task:t2"]
    # t2 is tracked, so a later delete is noticed
    assert state["known_ids"] == ["t1", "t2"]

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
    assert state["comments_refreshed_at"]["t1"] == 25 * HOUR_MS


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
    # incremental + one shared full listing
    assert len(session.task_listings()) == 2


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
    # stays, so the listing is not empty
    session.add_doc(make_doc("d3", 3_000), [])
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
    # 2 listings + pages
    assert len(session.paths(f"/v3/workspaces/{TEAM}/docs")) == 2 + 130


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


def test_shuffled_collections_produce_identical_markdown():
    """Shuffled assignees, tags, custom fields, checklists produce identical markdown."""
    task1 = make_task(
        "t1",
        1_000,
        assignees=[{"id": 2, "username": "Bob"}, {"id": 1, "username": "Alice"}],
        tags=[{"name": "urgent"}, {"name": "backend"}],
        custom_fields=[
            {"id": "cf2", "name": "Priority Code", "type": "short_text", "value": "P1"},
            {"id": "cf1", "name": "Estimated Hours", "type": "short_text", "value": "8"},
        ],
        checklists=[
            {
                "id": "chk2",
                "name": "Testing",
                "items": [{"id": "i2", "name": "Integration", "orderindex": 1}],
            },
            {
                "id": "chk1",
                "name": "Design",
                "items": [{"id": "i1", "name": "Mockup", "orderindex": 0}],
            },
        ],
    )
    task2 = make_task(
        "t1",
        1_000,
        assignees=[{"id": 1, "username": "Alice"}, {"id": 2, "username": "Bob"}],
        tags=[{"name": "backend"}, {"name": "urgent"}],
        custom_fields=[
            {"id": "cf1", "name": "Estimated Hours", "type": "short_text", "value": "8"},
            {"id": "cf2", "name": "Priority Code", "type": "short_text", "value": "P1"},
        ],
        checklists=[
            {
                "id": "chk1",
                "name": "Design",
                "items": [{"id": "i1", "name": "Mockup", "orderindex": 0}],
            },
            {
                "id": "chk2",
                "name": "Testing",
                "items": [{"id": "i2", "name": "Integration", "orderindex": 1}],
            },
        ],
    )
    md1 = _render_task_markdown(task1, "Engineering / Sprint 1")
    md2 = _render_task_markdown(task2, "Engineering / Sprint 1")
    assert md1 == md2


def test_short_page_does_not_stop_pagination_or_tombstone():
    """A short page in the middle of pagination does not stop paging or cause false tombstones."""
    # 150 tasks total: page 0 returns 99 tasks with last_page: False,
    # page 1 returns 51 tasks with last_page: True
    tasks = [make_task(f"t{i:03d}", 1000 + i) for i in range(150)]
    session = FakeClickUpSession(tasks)

    # Custom _tasks handler to simulate short page on page 0
    def scripted_tasks(params: dict[str, Any]) -> FakeResponse:
        page = int(params.get("page", 0))
        if page == 0:
            return FakeResponse(200, {"tasks": tasks[:99], "last_page": False})
        elif page == 1:
            return FakeResponse(200, {"tasks": tasks[99:], "last_page": True})
        return FakeResponse(200, {"tasks": [], "last_page": True})

    session._tasks = scripted_tasks  # type: ignore[method-assign]
    state: dict[str, Any] = {}
    rows = run_tasks(session, state, include_comments=False)

    assert len(rows) == 150
    assert not any(r.get("_deleted") for r in rows)
    assert len(state["known_ids"]) == 150


def test_stuck_docs_cursor_raises_clickup_error():
    """When /workspaces/{id}/docs returns the same next_cursor, ClickUpError is raised."""
    session = FakeClickUpSession()
    doc = make_doc("d1", 1_000)

    # Return same cursor repeatedly
    session.scripted = [
        FakeResponse(200, {"docs": [doc], "next_cursor": "cursor_stuck"}),
        FakeResponse(200, {"docs": [], "next_cursor": "cursor_stuck"}),
    ]

    with pytest.raises(ClickUpError, match="stuck on cursor"):
        list(clickup_module._paginate_docs(api_for(session), TEAM))


def test_two_sources_with_different_resource_names():
    """Two sources with different resource_name produce distinct tables and scope."""
    s1 = clickup_source(session=FakeClickUpSession(), resource_name="marketing")
    s2 = clickup_source(session=FakeClickUpSession(), resource_name="engineering")

    assert getattr(s1, PIPELINE_SCOPE_ATTR) == "marketing"
    assert getattr(s2, PIPELINE_SCOPE_ATTR) == "engineering"
    assert sorted(s1.resources) == ["marketing_docs", "marketing_tasks"]
    assert sorted(s2.resources) == ["engineering_docs", "engineering_tasks"]


def test_resource_cognee_sync_stats():
    source = clickup_source(session=FakeClickUpSession())
    tasks_res = source.resources["clickup_tasks"]
    docs_res = source.resources["clickup_docs"]
    assert "scanned" in tasks_res.cognee_sync_stats
    assert "failed_comments" in tasks_res.cognee_sync_stats
    assert "scanned" in docs_res.cognee_sync_stats
    assert "scanned" in source.cognee_sync_stats


def test_checklist_items_sorted_by_integer_orderindex():
    """Checklist items must be sorted by numeric orderindex, handling int and float strings."""
    task = make_task(
        "t_check",
        1_000,
        checklists=[
            {
                "name": "Sprint Tasks",
                "items": [
                    {"name": "Item 10", "orderindex": "10.000", "resolved": False},
                    {"name": "Item 1", "orderindex": "1.000", "resolved": True},
                    {"name": "Item 2", "orderindex": "2.000", "resolved": False},
                ],
            }
        ],
    )
    md = _render_task_markdown(task, "Eng")
    pos1 = md.find("Item 1")
    pos2 = md.find("Item 2")
    pos10 = md.find("Item 10")
    assert pos1 < pos2 < pos10, f"Expected Item 1 < Item 2 < Item 10 in markdown:\n{md}"


def test_doc_pages_sorted_by_integer_orderindex():
    """Doc pages must be sorted by numeric orderindex, handling int and float strings."""
    pages = [
        {"id": "p10", "name": "Page 10", "orderindex": "10.000", "pages": []},
        {"id": "p1", "name": "Page 1", "orderindex": "1.000", "pages": []},
        {"id": "p2", "name": "Page 2", "orderindex": "2.000", "pages": []},
    ]
    blocks = clickup_module._render_pages(pages)
    rendered = "\n".join(blocks)
    pos1 = rendered.find("Page 1")
    pos2 = rendered.find("Page 2")
    pos10 = rendered.find("Page 10")
    assert pos1 < pos2 < pos10


def test_comments_with_identical_timestamps_sorted_by_id_tiebreak():
    """Comments with the exact same timestamp or non-numeric dates are handled safely."""
    session = FakeClickUpSession([make_task("t1", 1_000)])
    session.comments["t1"] = [
        {"id": "c_zebra", "date": 1_700_000_000_000, "comment_text": "Zebra comment"},
        {"id": "c_apple", "date": 1_700_000_000_000, "comment_text": "Apple comment"},
        {"id": "c_bad_date", "date": "invalid-timestamp", "comment_text": "Bad date comment"},
    ]
    comments = _fetch_task_comments(api_for(session), "t1")
    assert comments is not None
    assert len(comments) == 3
    # Tie-break on same timestamp orders Apple before Zebra
    apple_pos = next(i for i, c in enumerate(comments) if c["text"] == "Apple comment")
    zebra_pos = next(i for i, c in enumerate(comments) if c["text"] == "Zebra comment")
    assert apple_pos < zebra_pos


def test_doc_without_date_updated_is_ingested_and_not_skipped():
    """Docs lacking date_updated are ingested on run 1, skipped on run 2, and updated on edit."""
    session = FakeClickUpSession()
    doc = {
        "id": "doc_no_date",
        "name": "Undated Doc",
        "date_created": 1690000000000,
        "date_updated": None,
        "deleted": False,
        "archived": False,
        "parent": {"id": "s1", "type": 4},
        "workspace_id": int(TEAM),
    }
    session.add_doc(doc, [{"id": "p1", "name": "Content", "content": "Version 1 text"}])
    state: dict[str, Any] = {}

    # Run 1: initial ingestion of undated doc
    rows1 = run_docs(session, state)
    assert len(rows1) == 1
    assert rows1[0]["id"] == "clickup:doc:doc_no_date"
    assert "Version 1 text" in rows1[0]["content"]
    assert "doc_no_date" in state["versions"]

    # Run 2: unchanged doc yields 0 rows (no churn)
    rows2 = run_docs(session, state)
    assert len(rows2) == 0

    # Run 3: page content is edited in ClickUp (doc still lacks date_updated)
    session.pages["doc_no_date"] = [{"id": "p1", "name": "Content", "content": "Version 2 text"}]
    rows3 = run_docs(session, state)
    assert len(rows3) == 1
    assert "Version 2 text" in rows3[0]["content"]

    # Run 4: doc title is renamed in ClickUp (doc still lacks date_updated)
    session.docs["doc_no_date"]["name"] = "Renamed Undated Doc"
    rows4 = run_docs(session, state)
    assert len(rows4) == 1
    assert rows4[0]["title"] == "Renamed Undated Doc"


def test_stored_comment_remains_after_403_and_retries_on_next_run(tmp_path: Path):
    """Task stored with comment, then renamed + status changed with 403 on comments:

    Run 2 keeps stored comment and title/status stay the old ones, id in comment_retry.
    Run 3 with comments readable: new title and status in table, comment_retry empty.
    """
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_task_403_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_t1_403.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_t1"),
    )

    t1 = make_task("t1", 1_000, name="Old title", closed=False)
    session = FakeClickUpSession([t1])
    session.comments["t1"] = [{"id": "c1", "date": 1_000, "comment_text": "stored decision"}]

    # Run 1: initial ingestion with comment
    source1 = clickup_source(session=session, include_docs=False)
    pipeline.run(source1)

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT title, content FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Old title"
        assert "**Status:** in progress" in row[1]
        assert "stored decision" in row[1]

    tasks_state1 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert tasks_state1["known_ids"] == ["t1"]
    assert tasks_state1["comment_retry"] == {}

    # Run 2: renamed + status changed, but comments endpoint returns 403 Forbidden
    session.tasks["t1"]["name"] = "New title"
    session.tasks["t1"]["status"] = {"status": "complete", "type": "closed"}
    session.tasks["t1"]["date_updated"] = 2_000
    # A newer task moves the cursor past t1, so only the retry-by-id can heal t1.
    session.add(make_task("t_newer", 3_000, name="Newer task"))
    original_comments = session._comments
    session._comments = lambda task_id, params: (  # type: ignore[method-assign]
        FakeResponse(403, {"err": "Comments restricted"})
        if task_id == "t1"
        else original_comments(task_id, params)
    )

    source2 = clickup_source(session=session, include_docs=False)
    pipeline.run(source2)

    assert source2.resources["clickup_tasks"].cognee_sync_stats["failed_comments"] == 1

    # Stored document stays as is: old title, old status, stored comments survive
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT title, content FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "Old title"
        assert "**Status:** in progress" in row[1]
        assert "stored decision" in row[1]

    tasks_state2 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert "t1" in tasks_state2["comment_retry"]
    assert tasks_state2["known_ids"] == ["t1", "t_newer"]
    assert tasks_state2["last_updated_ms"] == 3_000  # the cursor is now past t1

    # Run 3: comments readable again
    session._comments = original_comments  # type: ignore[method-assign]
    source3 = clickup_source(session=session, include_docs=False)
    pipeline.run(source3)

    # In database: new title and complete status now updated, comment still present
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT title, content FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "New title"
        assert "**Status:** complete" in row[1]
        assert "stored decision" in row[1]

    tasks_state3 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert tasks_state3["comment_retry"] == {}
    assert tasks_state3["known_ids"] == ["t1", "t_newer"]
    assert tasks_state3["last_updated_ms"] == 3_000


def test_new_task_with_403_comments_ingested_without_comments_and_healed_later(tmp_path: Path):
    """New task created while comments are 403, with newer task after it:

    Task is in table (no comments, hash 'unreadable') and in known_ids;
    when comments become readable the refresh adds them.
    """
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_new_task_403_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_new_403.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_new"),
    )

    t2 = make_task("t2", 1_000, name="Task Two")
    t3 = make_task("t3", 2_000, name="Task Three")
    session = FakeClickUpSession([t2, t3])

    def comments_handler(task_id: str, params: Any) -> FakeResponse:
        if task_id == "t2":
            return FakeResponse(403, {"err": "Comments restricted"})
        return FakeResponse(200, {"comments": [_comment(1, "comment on t3")]})

    session._comments = comments_handler  # type: ignore[method-assign]

    # Run 1: initial ingestion with t2 comments 403 and t3 comments 200
    source1 = clickup_source(session=session, include_docs=False)
    pipeline.run(source1)

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT id, title, content FROM {CLICKUP_TABLE_NAME} ORDER BY id"
        ) as cursor,
    ):
        rows = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        assert "clickup:task:t2" in rows
        assert rows["clickup:task:t2"][0] == "Task Two"
        assert "## Comments" not in rows["clickup:task:t2"][1]

        assert "clickup:task:t3" in rows
        assert rows["clickup:task:t3"][0] == "Task Three"
        assert "comment on t3" in rows["clickup:task:t3"][1]

    tasks_state1 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert tasks_state1["known_ids"] == ["t2", "t3"]
    assert tasks_state1["last_updated_ms"] == 2_000
    assert tasks_state1["comment_hashes"]["t2"] == "unreadable"
    assert "t2" not in tasks_state1["comment_retry"]

    # Run 2: comments readable again, refresh picks up t2 and heals comments
    del session._comments
    session.comments["t2"] = [_comment(2, "recovered comment for t2")]
    session.comments["t3"] = [_comment(1, "comment on t3")]

    source2 = clickup_source(session=session, include_docs=False, comment_refresh_hours=0.0)
    pipeline.run(source2)

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t2'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert "recovered comment for t2" in row[0]

    tasks_state2 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert tasks_state2["comment_hashes"]["t2"] != "unreadable"
    assert tasks_state2["known_ids"] == ["t2", "t3"]
    assert tasks_state2["last_updated_ms"] == 2_000
    assert tasks_state2["comment_retry"] == {}

    # Run 3: nothing changed, comments still readable: the healed row stays as it is
    pipeline.run(clickup_source(session=session, include_docs=False, comment_refresh_hours=0.0))
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {CLICKUP_TABLE_NAME} ORDER BY id") as cursor,
    ):
        rows3 = dict(cursor.fetchall())
    assert set(rows3) == {"clickup:task:t2", "clickup:task:t3"}
    assert "recovered comment for t2" in rows3["clickup:task:t2"]
    tasks_state3 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert tasks_state3["known_ids"] == ["t2", "t3"]
    assert tasks_state3["comment_retry"] == {}


def test_doc_pages_403_does_not_fail_pipeline_and_preserves_task(tmp_path: Path):
    """A doc whose pages return 403 next to a healthy task:

    The run completes, new task lands, doc and versions entry are unchanged, failed_docs == 1.
    """
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_doc_403_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_doc_403.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_doc_403"),
    )

    session = _doc_session()
    # Initial run: doc d1 and d2 stored
    pipeline.run(clickup_source(session=session))

    docs_state1 = pipeline.state["sources"]["clickup"]["resources"]["clickup_docs"]
    assert docs_state1["versions"]["d1"] == 1_000
    assert docs_state1["versions"]["d2"] == 2_000

    # Run 2: add healthy task t_new; doc d1 pages return 403 Forbidden
    session.add(make_task("t_new", 5_000, name="Healthy Task"))
    session.docs["d1"]["date_updated"] = 6_000
    session._doc_pages = lambda doc_id, params: (
        FakeResponse(  # type: ignore[method-assign]
            403, {"err": "Doc pages restricted"}
        )
        if doc_id == "d1"
        else None
    )

    source2 = clickup_source(session=session)
    pipeline.run(source2)

    assert source2.resources["clickup_docs"].cognee_sync_stats["failed_docs"] == 1

    # New task landed
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT id, title FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t_new'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert row[1] == "Healthy Task"

    # Stored doc d1 content remains intact, versions unchanged (not bumped to 6_000)
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content FROM {CLICKUP_DOCS_TABLE_NAME} WHERE id = 'clickup:doc:d1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert "We use **dlt**." in row[0]

    docs_state2 = pipeline.state["sources"]["clickup"]["resources"]["clickup_docs"]
    assert docs_state2["versions"]["d1"] == 1_000

    # Run 3: pages readable again, so the doc catches up to the version it missed
    session._doc_pages = None
    session.pages["d1"] = [{"id": "p1", "name": "Architecture", "content": "Now on dlt 1.9."}]
    source3 = clickup_source(session=session)
    pipeline.run(source3)
    assert source3.resources["clickup_docs"].cognee_sync_stats["failed_docs"] == 0
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content FROM {CLICKUP_DOCS_TABLE_NAME} WHERE id = 'clickup:doc:d1'"
        ) as cursor,
    ):
        assert "Now on dlt 1.9." in cursor.fetchone()[0]
    docs_state3 = pipeline.state["sources"]["clickup"]["resources"]["clickup_docs"]
    assert docs_state3["versions"]["d1"] == 6_000


def test_doc_pages_404_or_empty_body_does_not_tombstone(tmp_path: Path):
    """Doc listed but 404 on pages is not tombstoned; {} body keeps stored content."""
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_doc_404_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_doc_404.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_doc_404"),
    )

    session = _doc_session()
    # Run 1: initial ingestion of doc d1 and d2
    pipeline.run(clickup_source(session=session))

    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content FROM {CLICKUP_DOCS_TABLE_NAME} WHERE id = 'clickup:doc:d1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert "We use **dlt**." in row[0]

    # Run 2: doc d1 updated, but pages return 404
    session.docs["d1"]["date_updated"] = 2_000
    session._doc_pages = lambda doc_id, params: (
        FakeResponse(  # type: ignore[method-assign]
            404, {"err": "Not found"}
        )
        if doc_id == "d1"
        else None
    )

    pipeline.run(clickup_source(session=session))

    # Doc d1 is NOT tombstoned, remains present with original content
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content, _deleted FROM {CLICKUP_DOCS_TABLE_NAME} WHERE id = 'clickup:doc:d1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert "We use **dlt**." in row[0]
        assert not row[1]
    docs_state2 = pipeline.state["sources"]["clickup"]["resources"]["clickup_docs"]
    assert docs_state2["versions"]["d1"] == 1_000

    # Run 3: doc d1 updated again, but pages return 200 with {} (empty body)
    session.docs["d1"]["date_updated"] = 3_000
    session._doc_pages = lambda doc_id, params: (
        FakeResponse(  # type: ignore[method-assign]
            200, {}
        )
        if doc_id == "d1"
        else None
    )

    source3 = clickup_source(session=session)
    pipeline.run(source3)

    assert source3.resources["clickup_docs"].cognee_sync_stats["failed_docs"] == 1

    # Stored content unchanged (not overwritten with empty headers)
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT content, _deleted FROM {CLICKUP_DOCS_TABLE_NAME} WHERE id = 'clickup:doc:d1'"
        ) as cursor,
    ):
        row = cursor.fetchone()
        assert row is not None
        assert "We use **dlt**." in row[0]
        assert not row[1]
    docs_state3 = pipeline.state["sources"]["clickup"]["resources"]["clickup_docs"]
    assert docs_state3["versions"]["d1"] == 1_000


def test_refresh_records_refreshed_at_for_403_tasks_and_bounds_cost_unit():
    """All candidates 403 for several runs: cost stays bounded and healthy tasks get refreshed."""
    tasks = [make_task(f"t{i}", 100) for i in range(4)]
    session = FakeClickUpSession(tasks)
    state: dict[str, Any] = {}
    cache: dict[str, Any] = {}

    # Run 1: initial ingestion at now_ms = 0
    run_tasks(session, state, cache=cache, now_ms=0)
    assert set(state["known_ids"]) == {"t0", "t1", "t2", "t3"}

    # t0 and t1 comments permanently return 403; t2 and t3 return 200 with new comments
    def custom_comments(task_id: str, params: Any) -> FakeResponse:
        if task_id in ("t0", "t1"):
            return FakeResponse(403, {"err": "Restricted"})
        return FakeResponse(200, {"comments": [_comment(1, f"updated for {task_id}")]})

    session._comments = custom_comments  # type: ignore[method-assign]

    # Run 2: at 25h, batch size 2. t0 and t1 are tried, return 403, and stamped with refreshed_at
    session.calls.clear()
    now_25h = 25 * HOUR_MS
    run_tasks(
        session,
        state,
        cache=cache,
        now_ms=now_25h,
        comment_refresh_batch_size=2,
        detect_deletions=False,
    )
    assert state["comments_refreshed_at"]["t0"] == now_25h
    assert state["comments_refreshed_at"]["t1"] == now_25h

    # Run 3: immediate next run. t0 and t1 are NOT retried (within 24h window).
    # Healthy tasks t2 and t3 are refreshed without starvation!
    session.calls.clear()
    now_run3 = now_25h + 1_000
    rows3 = run_tasks(
        session,
        state,
        cache=cache,
        now_ms=now_run3,
        comment_refresh_batch_size=2,
        detect_deletions=False,
    )
    assert ids(rows3) == ["clickup:task:t2", "clickup:task:t3"]
    assert state["comments_refreshed_at"]["t2"] == now_run3
    assert state["comments_refreshed_at"]["t3"] == now_run3

    # Run 4: next run within window. All tasks have been refreshed, candidates list is empty!
    session.calls.clear()
    now_run4 = now_25h + 2_000
    rows4 = run_tasks(
        session,
        state,
        cache=cache,
        now_ms=now_run4,
        comment_refresh_batch_size=2,
        detect_deletions=False,
    )
    assert rows4 == []
    # No full task listing is executed when candidates is empty (bounded cost per run)
    full_listings = [
        params
        for p, params in session.calls
        if p == f"/v2/team/{TEAM}/task" and "date_updated_gt" not in params
    ]
    assert len(full_listings) == 0
    assert not [p for p, _ in session.calls if p.endswith("/comment")]
    assert len(session.calls) == 1


def test_check_active_via_source_and_guarded_rows():
    """A source with guarded_rows removed must fail a test that raises between two rows."""
    session = FakeClickUpSession([make_task("t1", 1_000), make_task("t2", 2_000)])

    row_count = 0

    def active_checker():
        nonlocal row_count
        # During API requests (space hierarchy + tasks listing), len(session.calls) <= 2.
        # Once the HTTP requests complete, rows are yielded. guarded_rows invokes
        # check_active() for each row boundary, allowing us to abort between rows!
        if len(session.calls) >= 2:
            row_count += 1
            if row_count >= 2:
                raise RuntimeError("Aborted between rows by guarded_rows")

    source = clickup_source(
        session=session,
        check_active=active_checker,
        include_comments=False,
        include_docs=False,
    )
    with pytest.raises(Exception, match="Aborted between rows by guarded_rows"):
        list(source.resources["clickup_tasks"])


def test_check_active_aborts_during_backoff_sleep():
    """check_active callback is polled and aborts during retry backoff sleep."""
    session = FakeClickUpSession()
    # 429 with 5s retry delay
    session.scripted = [FakeResponse(429, {"err": "Rate limited"}, {"Retry-After": "5"})]

    calls = 0

    def active_checker():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("Aborted during retry backoff")

    api = _ClickUpAPI(session, check_active=active_checker)
    with pytest.raises(RuntimeError, match="Aborted during retry backoff"):
        api.v2("/team")


def test_comment_refresh_is_bounded_by_batch_size():
    """Comment refresh must only refresh up to comment_refresh_batch_size tasks per run."""
    tasks = [make_task(f"t{i}", 1_000) for i in range(5)]
    session = FakeClickUpSession(tasks)
    for t in tasks:
        session.comments[t["id"]] = [{"id": f"c_{t['id']}", "date": 100, "comment_text": "text"}]

    state: dict[str, Any] = {}
    run_tasks(session, state)
    assert len(state["known_ids"]) == 5

    session.calls.clear()
    now = int(time.time() * 1000) + 25 * 3_600_000
    run_tasks(session, state, comment_refresh_batch_size=2, now_ms=now)
    comment_calls = [p for p in session.paths("/v2/task/") if p.endswith("/comment")]
    assert len(comment_calls) == 2


def test_retry_after_cap_at_60s():
    """Retry-After header greater than 60s must be capped at 60.0s."""
    delay = _extract_retry_delay({"Retry-After": "120"}, 1)
    assert delay == 60.0
    delay_large = _extract_retry_delay({"Retry-After": "3600"}, 1)
    assert delay_large == 60.0


def test_resource_name_validation():
    """Invalid resource_name raises ValueError with clear message."""
    session = FakeClickUpSession()
    for bad_name in (
        "_leading",
        "__x",
        "trailing_",
        "consecutive__underscores",
        "invalid-name",
        "has space",
        "bad.dot",
        "",
        "a" * 58,
        "1abc",
        "abc\n",
        "Foo",
        "ClickUp",
    ):
        with pytest.raises(ValueError, match=r"Invalid resource_name.*57 characters"):
            clickup_source(session=session, resource_name=bad_name)

    source = clickup_source(session=session, resource_name="valid_name_123")
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "valid_name_123"
    source_57 = clickup_source(session=session, resource_name="a" * 57)
    assert getattr(source_57, PIPELINE_SCOPE_ATTR) == "a" * 57


def test_task_pagination_guards_against_stuck_or_malformed_responses():
    """Task pagination must raise on missing tasks key or repeated page."""
    session = FakeClickUpSession()
    api = api_for(session)

    session.scripted = [FakeResponse(200, {"not_tasks": []})]
    with pytest.raises(ClickUpError, match="missing 'tasks'"):
        clickup_module._paginate_team_tasks(api, TEAM)

    t = make_task("t1", 1_000)
    session.scripted = [
        FakeResponse(200, {"tasks": [t]}),
        FakeResponse(200, {"tasks": [t]}),
    ]
    with pytest.raises(ClickUpError, match=r"(repeated page|stuck)"):
        clickup_module._paginate_team_tasks(api, TEAM)


def test_source_object_run_twice_gives_per_run_stats():
    """Running a source resource twice resets counters per run rather than accumulating."""
    task1 = make_task("t1", 1_000)
    task2 = make_task("t2", 2_000)
    session = FakeClickUpSession([task1, task2])

    source = clickup_source(session=session, include_docs=False)
    tasks_res = source.resources["clickup_tasks"]

    list(tasks_res)
    assert tasks_res.cognee_sync_stats["scanned"] == 2

    # Second run of the same resource object starts fresh
    list(tasks_res)
    assert tasks_res.cognee_sync_stats["scanned"] == 2


def test_unique_task_scanning_not_duplicated_in_stats():
    """Tasks seen in both incremental and full sweep listings are counted once in scanned stat."""
    task1 = make_task("t1", 1_000)
    task2 = make_task("t2", 2_000)
    session = FakeClickUpSession([task1, task2])

    state: dict[str, Any] = {}
    stats: dict[str, int] = {}
    # Run 1: initial ingestion
    run_tasks(session, state, stats=stats)
    assert stats["scanned"] == 2

    # Run 2: cursor is active, deletion sweep runs full listing
    session.tasks["t1"]["date_updated"] = 3_000
    stats2: dict[str, int] = {}
    run_tasks(session, state, stats=stats2, detect_deletions=True)
    # Both tasks in workspace were scanned, unique count is 2 (not 3 or 4)
    assert stats2["scanned"] == 2


def test_network_error_logs_class_name_without_url():
    """Transient network errors log the exception class name without exposing the URL/query."""
    import requests

    session = FakeClickUpSession()
    session.scripted = [requests.exceptions.ConnectionError("Connection reset by peer")]

    with patch.object(clickup_module.logger, "warning") as mock_warn:
        _request(session, f"{BASE}/v2/team/{TEAM}/task", params={"space_ids": "s1"})

    assert mock_warn.called
    warning_args = mock_warn.call_args[0]
    first_arg = warning_args[1]
    assert "ConnectionError" in str(first_arg)
    assert "space_ids" not in str(first_arg)
    assert "/v2/team" not in str(first_arg)


def test_refresh_records_refreshed_at_for_403_tasks_and_bounds_cost(tmp_path: Path):
    """Through dlt: permanently unreadable comments cannot starve healthy tasks or grow cost."""
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_refresh_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_refresh.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_refresh"),
    )
    session = FakeClickUpSession([make_task(f"t{i}", 100 + i) for i in range(4)])
    pipeline.run(clickup_source(session=session, include_docs=False))

    def comments(task_id: str, params: Any) -> FakeResponse:
        if task_id in ("t0", "t1"):
            return FakeResponse(403, {"err": "Restricted"})
        return FakeResponse(200, {"comments": [_comment(1, f"updated for {task_id}")]})

    session._comments = comments  # type: ignore[method-assign]

    def run() -> tuple[Any, list[str]]:
        session.calls.clear()
        source = clickup_source(
            session=session,
            include_docs=False,
            comment_refresh_hours=0.0,
            comment_refresh_batch_size=2,
        )
        pipeline.run(source)
        state = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
        return source, [p for p, _ in session.calls if p.endswith("/comment")], state

    # Run 2: the two least recently refreshed (t0, t1) are tried, fail, and are stamped.
    source2, comment_calls, state2 = run()
    assert sorted(comment_calls) == ["/v2/task/t0/comment", "/v2/task/t1/comment"]
    assert source2.resources["clickup_tasks"].cognee_sync_stats["failed_comments"] == 2
    stamp = state2["comments_refreshed_at"]["t0"]
    assert state2["comments_refreshed_at"]["t1"] == stamp

    # Run 3: t0/t1 now have the newest stamps, so the healthy tasks get the batch.
    _, comment_calls, _ = run()
    assert sorted(comment_calls) == ["/v2/task/t2/comment", "/v2/task/t3/comment"]
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {CLICKUP_TABLE_NAME} ORDER BY id") as cursor,
    ):
        rows = dict(cursor.fetchall())
    assert "updated for t2" in rows["clickup:task:t2"]
    assert "updated for t3" in rows["clickup:task:t3"]
    assert "## Comments" not in rows["clickup:task:t0"]  # stored doc of a 403 task untouched

    # Run 4: rotation continues; cost per run stays at batch_size comment requests.
    _, comment_calls, state4 = run()
    assert len(comment_calls) == 2
    assert state4["known_ids"] == ["t0", "t1", "t2", "t3"]
    assert state4["comment_retry"] == {}
    assert state4["last_updated_ms"] == 103


def test_retry_survives_403_on_the_task_itself(tmp_path: Path):
    """A retry whose GET /task/{id} is 403 keeps the stored task instead of failing the run."""
    import dlt

    pipeline = dlt.pipeline(
        pipeline_name="clickup_retry_task_403",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'clickup_retry.db').as_posix()}"
        ),
        dataset_name="clickup_ds",
        pipelines_dir=str(tmp_path / "dlt_state_retry"),
    )
    session = FakeClickUpSession([make_task("t1", 1_000, name="Stored title")])
    session.comments["t1"] = [_comment(1, "stored decision")]
    pipeline.run(clickup_source(session=session, include_docs=False))

    # Run 2: t1 edited while its comments are forbidden -> queued for retry.
    session.tasks["t1"]["name"] = "Edited title"
    session.tasks["t1"]["date_updated"] = 2_000
    original_comments = session._comments
    session._comments = lambda task_id, params: FakeResponse(  # type: ignore[method-assign]
        403, {"err": "Comments restricted"}
    )
    pipeline.run(clickup_source(session=session, include_docs=False))

    # Run 3: the task itself is now forbidden too. The run must still complete.
    session.forbidden_tasks.add("t1")
    session.add(make_task("t2", 3_000, name="Other task"))
    pipeline.run(clickup_source(session=session, include_docs=False))
    state3 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert "t1" in state3["comment_retry"]
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {CLICKUP_TABLE_NAME}") as cursor,
    ):
        rows = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
    assert rows["clickup:task:t1"][0] == "Stored title"
    assert "stored decision" in rows["clickup:task:t1"][1]
    assert rows["clickup:task:t2"][0] == "Other task"

    # Run 4: everything readable again -> t1 healed, retry queue empty.
    session.forbidden_tasks.clear()
    session._comments = original_comments  # type: ignore[method-assign]
    pipeline.run(clickup_source(session=session, include_docs=False))
    state4 = pipeline.state["sources"]["clickup"]["resources"]["clickup_tasks"]
    assert state4["comment_retry"] == {}
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            f"SELECT title FROM {CLICKUP_TABLE_NAME} WHERE id = 'clickup:task:t1'"
        ) as cursor,
    ):
        assert cursor.fetchone()[0] == "Edited title"


def test_comment_retry_takes_the_oldest_attempts_first(monkeypatch):
    """With more failing tasks than the per-run cap, every task still gets retried in turn."""
    monkeypatch.setattr(clickup_module, "_MAX_COMMENT_RETRIES_PER_RUN", 2)
    session = FakeClickUpSession([make_task(t, 100) for t in ("a", "b", "c")])
    state: dict[str, Any] = {}
    run_tasks(session, state, now_ms=0)  # all three stored normally
    session._comments = lambda task_id, params: FakeResponse(  # type: ignore[method-assign]
        403, {"err": "Restricted"}
    )
    state["comment_retry"] = {"a": 300, "b": 100, "c": 200}

    def retried_ids(now: int) -> list[str]:
        session.calls.clear()
        run_tasks(session, state, now_ms=now, comment_refresh_hours=None, detect_deletions=False)
        return [p.split("/")[3] for p, _ in session.calls if p.endswith("/comment")]

    assert retried_ids(1_000) == ["b", "c"]  # the two oldest attempts
    assert retried_ids(2_000) == ["a", "b"]  # a now oldest; b/c were stamped 1_000


def test_shuffled_repeat_page_is_detected_on_the_first_repeat():
    """The same ids in a different order must stop paging at once, not after many pages."""
    session = FakeClickUpSession([make_task(f"t{i}", 100 + i) for i in range(3)])
    rotations = [["t0", "t1", "t2"], ["t1", "t2", "t0"], ["t2", "t0", "t1"], ["t0", "t2", "t1"]]

    def endless_shuffle(params: dict[str, Any]) -> FakeResponse:
        order = rotations[int(params.get("page", 0)) % len(rotations)]
        return FakeResponse(200, {"tasks": [session.tasks[t] for t in order], "last_page": False})

    session._tasks = endless_shuffle  # type: ignore[method-assign]
    with pytest.raises(clickup_module.ClickUpError, match="stuck"):
        run_tasks(session, {})
    assert len(session.task_listings()) == 2  # page 0, then the first (shuffled) repeat
