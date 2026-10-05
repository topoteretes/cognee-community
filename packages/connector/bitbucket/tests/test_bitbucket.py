"""Offline tests for the Bitbucket Cloud DLT source and sync state machine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from cognee_community_connector_bitbucket.bitbucket import (
    BITBUCKET_SOURCE_NAME,
    BitbucketAPIError,
    _BitbucketClient,
    _paginate,
    _sync_documents,
    bitbucket_source,
)


def _pr(number: int, *, updated_on: str = "2026-09-01T00:00:00+00:00", title=None):
    return {
        "id": number,
        "title": title or f"PR {number}",
        "description": {"raw": f"Details for PR {number}"},
        "state": "OPEN",
        "author": {"display_name": "Ada"},
        "source": {"branch": {"name": "feature"}},
        "destination": {"branch": {"name": "main"}},
        "updated_on": updated_on,
        "links": {"html": {"href": f"https://bitbucket.org/acme/repo/pull-requests/{number}"}},
    }


def _comment(comment_id: int, *, updated_on="2026-09-01T00:00:00+00:00", text="Review note"):
    return {
        "id": comment_id,
        "content": {"raw": text},
        "updated_on": updated_on,
        "created_on": updated_on,
        "links": {
            "html": {
                "href": f"https://bitbucket.org/acme/repo/pull-requests/1#comment-{comment_id}"
            }
        },
    }


class _Response:
    def __init__(self, payload=None, *, status=200, text="", content=None):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")

    def json(self):
        return deepcopy(self._payload)


class FakeBitbucketSession:
    """Requests-compatible fake serving a mutable workspace snapshot."""

    def __init__(self, *, prs=None, comments=None, wiki=None, has_wiki=True, repo_meta=None):
        self.prs = list(prs or [])
        self.comments = dict(comments or {})
        self.wiki = dict(wiki or {})
        self.has_wiki = has_wiki
        self.repo_meta = repo_meta
        self.headers = {}
        self.auth = None
        self.calls = []
        self.fail_on = None
        self.force_status = None
        self.paginate = True

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, deepcopy(params), timeout))
        if self.fail_on and self.fail_on in url:
            return _Response(
                {"error": "token should never be printed"}, status=self.force_status or 500
            )
        if self.force_status:
            return _Response(status=self.force_status)

        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        for key, value in params.items():
            query[key] = value if isinstance(value, list) else [value]
        values = []

        if path == "/2.0/repositories/acme/repo":
            metadata = self.repo_meta if self.repo_meta is not None else {"has_wiki": self.has_wiki}
            return _Response(metadata)
        if path == "/2.0/repositories/acme/repo/pullrequests":
            values = list(self.prs)
            if "q" in query:
                # The connector sends an inclusive ISO timestamp boundary.
                quoted_cursor = query["q"][0].split('"')[1]
                cursor = datetime.fromisoformat(quoted_cursor)
                values = [pr for pr in values if datetime.fromisoformat(pr["updated_on"]) >= cursor]
        elif (
            path.startswith("/2.0/repositories/acme/repo/pullrequests/")
            and path.rsplit("/", 1)[-1].isdigit()
        ):
            pr_id = int(path.rsplit("/", 1)[-1])
            pr = next((item for item in self.prs if item["id"] == pr_id), None)
            if pr is None:
                return _Response({"error": "not found"}, status=404)
            return _Response(pr)
        elif path == "/2.0/repositories/acme/repo.wiki/src/HEAD/":
            values = [
                {"path": item_path, "type": "commit_file"}
                for item_path in self.wiki
                if "/" not in item_path
            ]
            if any(item_path.startswith("docs/") for item_path in self.wiki):
                values.append({"path": "docs", "type": "commit_directory"})
            if any(item_path.startswith("images/") for item_path in self.wiki):
                values.append({"path": "images", "type": "commit_directory"})
        elif path in (
            "/2.0/repositories/acme/repo.wiki/src/HEAD/docs",
            "/2.0/repositories/acme/repo.wiki/src/HEAD/images",
        ):
            directory = path.rsplit("/", 1)[-1]
            values = [
                {"path": item_path, "type": "commit_file"}
                for item_path in self.wiki
                if item_path.startswith(f"{directory}/")
            ]
        elif path.endswith("/comments"):
            pr_number = path.split("/")[-2]
            values = list(self.comments.get(int(pr_number), []))
        elif "/repo.wiki/src/HEAD/" in path:
            file_path = path.split("/repo.wiki/src/HEAD/", 1)[1]
            content = self.wiki.get(file_path, "")
            if isinstance(content, bytes):
                return _Response(content=content)
            return _Response(text=content)
        else:
            raise AssertionError(f"Unexpected Bitbucket API path: {path}")

        page = int(query.get("page", ["1"])[0])
        if not self.paginate:
            return _Response({"values": values})
        # Keep pages intentionally small so tests exercise opaque next links.
        start = page - 1
        current = values[start : start + 1]
        next_url = None
        if start + 1 < len(values):
            next_url = f"https://api.bitbucket.org{path}?page={page + 1}"
        return _Response({"values": deepcopy(current), "next": next_url})


def _client(session):
    return _BitbucketClient(session, email=None, api_token=None)


def test_authentication_uses_basic_credentials_without_logging_them():
    session = FakeBitbucketSession()
    _BitbucketClient(session, email="user@example.com", api_token="sensitive-api-token")

    assert session.auth == ("user@example.com", "sensitive-api-token")
    assert session.headers["Accept"] == "application/json"


def test_factory_requires_credentials_and_valid_selection():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="credentials required"):
        bitbucket_source(workspace="acme", repositories=["repo"])
    with pytest.raises(ValueError, match="Unsupported Bitbucket content type"):
        bitbucket_source(
            workspace="acme",
            repositories=["repo"],
            access_token="test-token",
            content_types=["issues"],
        )
    with pytest.raises(ValueError, match="at least one content type"):
        bitbucket_source(
            workspace="acme",
            repositories=["repo"],
            access_token="test-token",
            content_types=[],
        )


def test_factory_creates_document_source_with_selected_inputs(monkeypatch):
    pytest.importorskip("dlt")
    __import__("dlt.sources.helpers.requests")  # Register DLT's requests subclass first.
    import requests

    session = FakeBitbucketSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    source = bitbucket_source(
        workspace="acme",
        repositories=["repo", "repo"],
        content_types=["pull_requests"],
        access_token="oauth-token",
    )

    assert source.name == BITBUCKET_SOURCE_NAME
    assert source.resources["bitbucket_documents"]
    assert source.cognee_document_source == "bitbucket"
    assert session.headers["Authorization"] == "Bearer oauth-token"
    schema = source.resources["bitbucket_documents"].compute_table_schema()
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"

    supplied_session = FakeBitbucketSession()
    bitbucket_source(
        workspace="acme",
        repositories=["repo"],
        access_token="explicit-token",
        session=supplied_session,
    )
    assert supplied_session.headers["Authorization"] == "Bearer explicit-token"


def test_pagination_follows_opaque_next_links_and_deduplicates_sync_rows():
    session = FakeBitbucketSession(prs=[_pr(1), _pr(2)])
    rows = _sync_documents(
        _client(session),
        {},
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests"},
    )

    assert [row["id"] for row in rows] == [
        "acme/repo:pullrequest:1",
        "acme/repo:pullrequest:2",
    ]
    assert any("?page=2" in call[0] for call in session.calls)
    assert all(params.get("pagelen", 50) <= 50 for _url, params, _timeout in session.calls)
    assert all("fields" not in params for _url, params, _timeout in session.calls)
    assert all(row["source_type"] == "pull_request" for row in rows)


def test_paginate_stops_on_empty_page_and_rejects_repeated_links():
    class Client:
        def __init__(self, payloads):
            self.payloads = iter(payloads)

        def get(self, _url, params=None):
            return next(self.payloads)

    assert list(_paginate(Client([{"values": []}]), "start")) == []
    with pytest.raises(BitbucketAPIError, match="unexpected paginated"):
        list(_paginate(Client([{}]), "start"))
    with pytest.raises(BitbucketAPIError, match="invalid pagination link"):
        list(_paginate(Client([{"values": [], "next": ""}]), "start"))
    with pytest.raises(BitbucketAPIError, match="incomplete empty page"):
        list(_paginate(Client([{"values": [], "size": 3}]), "start"))
    with pytest.raises(BitbucketAPIError, match="repeated next-page"):
        list(
            _paginate(
                Client(
                    [
                        {"values": [{"id": 1}], "next": "same"},
                        {"values": [{"id": 2}], "next": "same"},
                    ]
                ),
                "start",
            )
        )


def test_empty_results_are_valid_and_state_remains_empty():
    session = FakeBitbucketSession(prs=[], comments={}, wiki={}, has_wiki=False)
    state = {}

    assert (
        _sync_documents(
            _client(session),
            state,
            workspace="acme",
            repositories=["repo"],
            content_types={"pull_requests", "comments", "wiki"},
        )
        == []
    )
    assert state == {
        "known_ids_by_scope": {
            "acme/repo:pull_requests": [],
            "acme/repo:comments": [],
            "acme/repo:wiki": [],
        },
        "fingerprints": {},
        "last_sync_by_repo": {"acme/repo": ""},
    }


def test_selected_prs_comments_and_wiki_are_documents_with_stable_ids():
    session = FakeBitbucketSession(
        prs=[_pr(1)],
        comments={1: [_comment(9)]},
        wiki={
            "Home.md": "Wiki landing page",
            "docs/Guide.md": "Setup café guide",
            "images/logo.png": b"\x89PNG\x00binary",
        },
    )
    rows = _sync_documents(
        _client(session),
        {},
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments", "wiki"},
    )

    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {
        "acme/repo:pullrequest:1",
        "acme/repo:pullrequest:1:comment:9",
        "acme/repo:wiki:Home.md",
        "acme/repo:wiki:docs/Guide.md",
    }
    assert "PR 1" in by_id["acme/repo:pullrequest:1"]["content"]
    assert by_id["acme/repo:pullrequest:1:comment:9"]["content"] == "Review note"
    assert by_id["acme/repo:wiki:docs/Guide.md"]["content"] == "Setup café guide"
    assert "acme/repo:wiki:images/logo.png" not in by_id


def test_wiki_only_selection_does_not_call_pull_request_api():
    session = FakeBitbucketSession(wiki={"Home.md": "Wiki landing page"})

    rows = _sync_documents(
        _client(session),
        {},
        workspace="acme",
        repositories=["repo"],
        content_types={"wiki"},
    )

    assert [row["id"] for row in rows] == ["acme/repo:wiki:Home.md"]
    assert all("/pullrequests" not in call[0] for call in session.calls)


def test_deselecting_content_type_does_not_delete_its_existing_documents():
    session = FakeBitbucketSession(
        prs=[_pr(1)],
        comments={1: [_comment(9)]},
        wiki={"Home.md": "Wiki landing page"},
    )
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments", "wiki"},
    )

    rows = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests"},
    )

    assert rows == []
    current_ids = set().union(*map(set, state["known_ids_by_scope"].values()))
    assert "acme/repo:pullrequest:1:comment:9" in current_ids
    assert "acme/repo:wiki:Home.md" in current_ids


def test_repeated_sync_emits_only_changed_content_and_uses_updated_on_filter():
    session = FakeBitbucketSession(prs=[_pr(1)], comments={1: [_comment(9)]})
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments"},
    )
    session.calls.clear()

    assert (
        _sync_documents(
            client,
            state,
            workspace="acme",
            repositories=["repo"],
            content_types={"pull_requests", "comments"},
        )
        == []
    )
    assert any("updated_on >=" in str(params.get("q")) for _, params, _ in session.calls)

    session.prs = [_pr(1, updated_on="2026-09-02T00:00:00+00:00", title="Updated PR")]
    changed = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments"},
    )
    assert [row["id"] for row in changed] == ["acme/repo:pullrequest:1"]
    assert state["last_sync_by_repo"]["acme/repo"].startswith("2026-09-02")


def test_same_timestamp_boundary_includes_all_records_without_duplicate_emission():
    boundary = "2026-09-01T00:00:00+00:00"
    session = FakeBitbucketSession(prs=[_pr(1, updated_on=boundary), _pr(2, updated_on=boundary)])
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests"},
    )

    session.prs[1] = _pr(2, updated_on=boundary, title="Changed at the same timestamp")
    changed = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests"},
    )

    assert [row["id"] for row in changed] == ["acme/repo:pullrequest:2"]


def test_comment_changes_are_detected_even_when_parent_pr_timestamp_does_not_change():
    session = FakeBitbucketSession(prs=[_pr(1)], comments={1: [_comment(9)]})
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments"},
    )
    session.comments[1] = [_comment(9, updated_on="2026-09-02T00:00:00+00:00", text="Edited")]

    changed = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments"},
    )

    assert [row["id"] for row in changed] == ["acme/repo:pullrequest:1:comment:9"]
    assert changed[0]["content"] == "Edited"


def test_deleted_comment_tombstone_emits_hard_delete_not_empty_document():
    session = FakeBitbucketSession(prs=[_pr(1)], comments={1: [_comment(9)]})
    state = {}
    initial = _sync_documents(
        _client(session),
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"comments"},
    )
    assert [row["id"] for row in initial] == ["acme/repo:pullrequest:1:comment:9"]

    deleted_comment = _comment(9)
    deleted_comment.update({"deleted": True, "content": {"raw": "", "html": ""}})
    session.comments[1] = [deleted_comment]
    deleted = _sync_documents(
        _client(session),
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"comments"},
    )

    assert deleted == [{"id": "acme/repo:pullrequest:1:comment:9", "_deleted": True}]


def test_deleted_pr_comment_and_wiki_page_emit_tombstones_only_after_inventory():
    session = FakeBitbucketSession(
        prs=[_pr(1), _pr(2)],
        comments={1: [_comment(9)], 2: [_comment(10)]},
        wiki={"Home.md": "Home", "Old.md": "Old page"},
    )
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments", "wiki"},
    )
    session.prs = [_pr(1)]
    session.comments = {1: []}
    session.wiki = {"Home.md": "Home"}

    rows = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments", "wiki"},
    )

    assert rows == [
        {"id": "acme/repo:pullrequest:1:comment:9", "_deleted": True},
        {"id": "acme/repo:pullrequest:2", "_deleted": True},
        {"id": "acme/repo:pullrequest:2:comment:10", "_deleted": True},
        {"id": "acme/repo:wiki:Old.md", "_deleted": True},
    ]


def test_api_failure_keeps_state_unchanged_and_does_not_return_deletion_rows(monkeypatch):
    import cognee_community_connector_bitbucket.bitbucket as bitbucket_module

    monkeypatch.setattr(bitbucket_module.time, "sleep", lambda _delay: None)
    identity = "acme/repo:pullrequest:1"
    state = {
        "known_ids_by_scope": {"acme/repo:pull_requests": [identity]},
        "fingerprints": {identity: "old-hash"},
        "last_sync_by_repo": {"acme/repo": "2026-09-01T00:00:00+00:00"},
    }
    original = deepcopy(state)
    session = FakeBitbucketSession(prs=[])
    session.fail_on = "/pullrequests"
    with pytest.raises(BitbucketAPIError, match="HTTP 500"):
        _sync_documents(
            _client(session),
            state,
            workspace="acme",
            repositories=["repo"],
            content_types={"pull_requests"},
        )
    assert state == original


def test_mid_pagination_failure_cannot_emit_tombstones_or_advance_state(monkeypatch):
    import cognee_community_connector_bitbucket.bitbucket as bitbucket_module

    monkeypatch.setattr(bitbucket_module.time, "sleep", lambda _delay: None)
    prior_id = "acme/repo:pullrequest:99"
    state = {
        "known_ids_by_scope": {"acme/repo:pull_requests": [prior_id]},
        "fingerprints": {prior_id: "prior-hash"},
        "last_sync_by_repo": {"acme/repo": "2026-08-31T00:00:00+00:00"},
    }
    original = deepcopy(state)
    session = FakeBitbucketSession(prs=[_pr(1), _pr(2)])
    session.fail_on = "page=2"

    with pytest.raises(BitbucketAPIError, match="HTTP 500"):
        _sync_documents(
            _client(session),
            state,
            workspace="acme",
            repositories=["repo"],
            content_types={"pull_requests"},
        )

    assert state == original


def test_repository_and_content_type_scopes_do_not_delete_unselected_documents():
    class MultiRepoSession:
        def __init__(self):
            self.headers = {}
            self.auth = None
            self.prs = {"repo-a": [_pr(1)], "repo-b": [_pr(2)]}
            self.calls = []

        def get(self, url, params=None, timeout=None):
            parsed = urlparse(url)
            parts = parsed.path.split("/")
            repo = parts[4]
            self.calls.append((url, deepcopy(params or {}), timeout))
            if parts[-1] == "pullrequests":
                values = self.prs[repo]
                query = (params or {}).get("q")
                if query:
                    cursor = query.split('"')[1]
                    values = [item for item in values if item["updated_on"] >= cursor]
                return _Response({"values": deepcopy(values)})
            if parts[-2] == "pullrequests":
                pr_id = int(parts[-1])
                return _Response(next(item for item in self.prs[repo] if item["id"] == pr_id))
            raise AssertionError(f"Unexpected multi-repository path: {parsed.path}")

    client = _client(MultiRepoSession())
    state = {}
    initial = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo-a", "repo-b"],
        content_types={"pull_requests"},
    )
    assert len(initial) == 2

    rows = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo-a"],
        content_types={"pull_requests"},
    )

    assert rows == []
    assert "acme/repo-b:pullrequest:2" in state["known_ids_by_scope"]["acme/repo-b:pull_requests"]


def test_pull_request_cursors_are_per_repository_and_newly_visible_old_prs_are_fetched():
    class MultiRepoSession:
        def __init__(self):
            self.headers = {}
            self.auth = None
            self.prs = {
                "repo-a": [_pr(1, updated_on="2026-09-10T00:00:00+00:00")],
                "repo-b": [_pr(2, updated_on="2026-09-02T00:00:00+00:00")],
            }
            self.queries = {}
            self.detail_calls = []

        def get(self, url, params=None, timeout=None):
            parsed = urlparse(url)
            parts = parsed.path.split("/")
            repo = parts[4]
            if parts[-1] == "pullrequests":
                values = self.prs[repo]
                query = (params or {}).get("q")
                if query:
                    self.queries[repo] = query
                    cursor = query.split('"')[1]
                    values = [item for item in values if item["updated_on"] >= cursor]
                return _Response({"values": deepcopy(values)})
            if parts[-2] == "pullrequests":
                self.detail_calls.append(parsed.path)
                pr_id = int(parts[-1])
                return _Response(next(item for item in self.prs[repo] if item["id"] == pr_id))
            raise AssertionError(f"Unexpected multi-repository path: {parsed.path}")

    session = MultiRepoSession()
    client = _client(session)
    state = {}
    _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo-a", "repo-b"],
        content_types={"pull_requests"},
    )
    assert state["last_sync_by_repo"]["acme/repo-a"].startswith("2026-09-10")
    assert state["last_sync_by_repo"]["acme/repo-b"].startswith("2026-09-02")
    assert session.detail_calls == []

    session.prs["repo-b"] = [_pr(2, updated_on="2026-09-03T00:00:00+00:00", title="Changed")]
    session.detail_calls.clear()
    changed = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo-a", "repo-b"],
        content_types={"pull_requests"},
    )
    assert [row["id"] for row in changed] == ["acme/repo-b:pullrequest:2"]
    assert '"2026-09-02T00:00:00.000000+00:00"' in session.queries["repo-b"]
    assert session.detail_calls == []

    session.prs["repo-b"].append(_pr(3, updated_on="2025-01-01T00:00:00+00:00"))
    session.detail_calls.clear()
    newly_visible = _sync_documents(
        client,
        state,
        workspace="acme",
        repositories=["repo-a", "repo-b"],
        content_types={"pull_requests"},
    )
    assert "acme/repo-b:pullrequest:3" in [row["id"] for row in newly_visible]
    assert session.detail_calls == ["/2.0/repositories/acme/repo-b/pullrequests/3"]


def test_http_auth_errors_are_clear_and_do_not_include_response_or_token():
    session = FakeBitbucketSession()
    session.force_status = 401
    client = _client(session)

    with pytest.raises(BitbucketAPIError) as error:
        client.get("repositories/acme/repo")

    assert "authentication failed" in str(error.value)
    assert "token should never be printed" not in str(error.value)
    assert "sensitive-api-token" not in str(error.value)


def test_forbidden_response_explains_scope_problem():
    session = FakeBitbucketSession()
    session.force_status = 403
    with pytest.raises(BitbucketAPIError, match="required read scopes"):
        _client(session).get("repositories/acme/repo")
    assert len(session.calls) == 1


def test_rate_limit_retries_and_honors_retry_after(monkeypatch):
    import cognee_community_connector_bitbucket.bitbucket as bitbucket_module

    delays = []
    monkeypatch.setattr(bitbucket_module.time, "sleep", delays.append)

    class OnceLimited(FakeBitbucketSession):
        limited = False

        def get(self, url, params=None, timeout=None):
            if not self.limited:
                self.limited = True
                response = _Response(status=429)
                response.headers["Retry-After"] = "0.25"
                self.calls.append((url, deepcopy(params or {}), timeout))
                return response
            return super().get(url, params=params, timeout=timeout)

    session = OnceLimited(prs=[_pr(1)])
    payload = _client(session).get("repositories/acme/repo")

    assert payload == {"has_wiki": True}
    assert len(session.calls) == 2
    assert delays == [0.25]


def test_server_error_retries_are_bounded_and_eventually_surface(monkeypatch):
    import cognee_community_connector_bitbucket.bitbucket as bitbucket_module

    delays = []
    monkeypatch.setattr(bitbucket_module.time, "sleep", delays.append)
    session = FakeBitbucketSession()
    session.force_status = 500

    with pytest.raises(BitbucketAPIError, match="HTTP 500"):
        _client(session).get("repositories/acme/repo")

    assert len(session.calls) == 5
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_request_exception_message_does_not_leak_credentials(monkeypatch):
    import requests

    import cognee_community_connector_bitbucket.bitbucket as bitbucket_module

    monkeypatch.setattr(bitbucket_module.time, "sleep", lambda _delay: None)

    class BrokenSession(FakeBitbucketSession):
        def get(self, _url, params=None, timeout=None):
            raise requests.exceptions.ConnectionError("failed with sensitive-api-token")

    with pytest.raises(BitbucketAPIError) as error:
        _client(BrokenSession()).get("repositories/acme/repo")

    assert "sensitive-api-token" not in str(error.value)


def test_malformed_pagination_payload_fails_closed():
    class Client:
        def get(self, _url, params=None):
            return {"values": {"unexpected": "not-a-list"}}

    with pytest.raises(BitbucketAPIError, match="unexpected paginated"):
        list(_paginate(Client(), "repositories/acme/repo/pullrequests"))


def test_missing_has_wiki_allows_pr_comment_sync_and_preserves_wiki_inventory():
    pr_id = "acme/repo:pullrequest:1"
    comment_id = "acme/repo:pullrequest:1:comment:9"
    prior_id = "acme/repo:wiki:Home.md"
    state = {
        "known_ids_by_scope": {
            "acme/repo:pull_requests": [pr_id],
            "acme/repo:comments": [comment_id],
            "acme/repo:wiki": [prior_id],
        },
        "fingerprints": {
            pr_id: "old-pr-hash",
            comment_id: "old-comment-hash",
            prior_id: "old-wiki-hash",
        },
    }
    session = FakeBitbucketSession(
        prs=[_pr(1)],
        comments={1: [_comment(9)]},
        # This matches the live repository response shape: ordinary metadata,
        # but no has_wiki field.
        repo_meta={"type": "repository", "slug": "repo", "full_name": "acme/repo"},
    )

    rows = _sync_documents(
        _client(session),
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"pull_requests", "comments", "wiki"},
    )

    assert {row["id"] for row in rows} == {pr_id, comment_id}
    assert all(row.get("_deleted") is not True for row in rows)
    assert state["known_ids_by_scope"]["acme/repo:wiki"] == [prior_id]
    assert state["fingerprints"][prior_id] == "old-wiki-hash"
    assert not any(".wiki/src/" in url for url, _, _ in session.calls)


def test_explicit_has_wiki_false_completes_empty_inventory_and_deletes_old_wiki():
    prior_id = "acme/repo:wiki:Home.md"
    state = {
        "known_ids_by_scope": {"acme/repo:wiki": [prior_id]},
        "fingerprints": {prior_id: "old-hash"},
    }
    session = FakeBitbucketSession(prs=[], has_wiki=False)

    rows = _sync_documents(
        _client(session),
        state,
        workspace="acme",
        repositories=["repo"],
        content_types={"wiki"},
    )

    assert rows == [{"id": prior_id, "_deleted": True}]
    assert state["known_ids_by_scope"]["acme/repo:wiki"] == []
    assert prior_id not in state["fingerprints"]


@pytest.mark.parametrize("has_wiki", [None, "false", 0])
def test_malformed_has_wiki_value_fails_closed_without_advancing_state(has_wiki):
    prior_id = "acme/repo:wiki:Home.md"
    state = {
        "known_ids_by_scope": {"acme/repo:wiki": [prior_id]},
        "fingerprints": {prior_id: "old-hash"},
    }
    original = deepcopy(state)
    session = FakeBitbucketSession(prs=[_pr(1)], repo_meta={"has_wiki": has_wiki})

    with pytest.raises(BitbucketAPIError, match="invalid has_wiki"):
        _sync_documents(
            _client(session),
            state,
            workspace="acme",
            repositories=["repo"],
            content_types={"pull_requests", "wiki"},
        )

    assert state == original


def test_real_dlt_merge_removes_deleted_source_records(tmp_path):
    dlt = pytest.importorskip("dlt")
    pipeline = dlt.pipeline(
        pipeline_name=f"bitbucket_e2e_{tmp_path.name.replace('-', '_')}",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'bitbucket.db'}"),
        dataset_name="bitbucket_test",
        pipelines_dir=str(tmp_path / "dlt_pipelines"),
    )

    first = FakeBitbucketSession(prs=[_pr(1), _pr(2)])
    pipeline.run(
        bitbucket_source(
            workspace="acme",
            repositories=["repo"],
            content_types=["pull_requests"],
            session=first,
        )
    )
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM bitbucket_documents")[0][0] == 2

    # The second snapshot has only PR 1. Its stable hash is unchanged; PR 2 is
    # absent from the complete inventory and becomes a hard-delete tombstone.
    second = FakeBitbucketSession(prs=[_pr(1)])
    pipeline.run(
        bitbucket_source(
            workspace="acme",
            repositories=["repo"],
            content_types=["pull_requests"],
            session=second,
        )
    )
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM bitbucket_documents")
    assert [row[0] for row in rows] == ["acme/repo:pullrequest:1"]
