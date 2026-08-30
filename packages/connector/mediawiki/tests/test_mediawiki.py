"""Unit tests for the MediaWiki connector without live HTTP requests."""

from copy import deepcopy

import pytest

from cognee_community_connector_mediawiki.mediawiki import (
    _CURSOR_KEY,
    _SEEN_RCIDS_KEY,
    _build_config,
    _fetch_page,
    _iter_rows,
    _page_to_row,
    _wikitext_to_plain_text,
)

API_URL = "https://wiki.example.test/w/api.php"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return deepcopy(self._payload)


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(dict(params))
        return FakeResponse(self.handler(dict(params)))


def _config(**overrides):
    defaults = {
        "api_url": API_URL,
        "page_titles": ["Alpha"],
        "page_prefix": None,
        "namespaces": (0,),
        "timeout": 5.0,
        "overlap_seconds": 10,
    }
    defaults.update(overrides)
    return _build_config(**defaults)


def _page(
    page_id,
    title,
    content="'''Alpha''' links to [[Beta|the beta page]].\n\n{{Infobox}}",
    categories=("Category:Examples",),
):
    return {
        "pageid": int(page_id),
        "ns": 0,
        "title": title,
        "fullurl": f"https://wiki.example.test/wiki/{title}",
        "revisions": [
            {
                "revid": 200 + int(page_id),
                "parentid": 100 + int(page_id),
                "timestamp": "2026-08-30T10:00:00Z",
                "user": "Editor",
                "comment": "update",
                "slots": {"main": {"content": content}},
            }
        ],
        "categories": [{"title": category} for category in categories],
    }


def _page_response(page):
    return {"batchcomplete": True, "query": {"pages": [page]}}


def _server_time(value):
    return {"batchcomplete": True, "curtimestamp": value}


def _recent_changes(changes, continuation=None):
    payload = {"batchcomplete": True, "query": {"recentchanges": changes}}
    if continuation:
        payload["continue"] = continuation
    return payload


def test_wikitext_is_converted_to_plain_text():
    text = _wikitext_to_plain_text("== Heading ==\n'''Bold''' and [[Target|label]]. {{Drop me}}")

    assert "Heading" in text
    assert "Bold and label." in text
    assert "Drop me" not in text
    assert "[[" not in text


def test_fetch_page_collects_paginated_categories():
    first = _page("1", "Alpha", categories=("Category:One",))
    second = _page("1", "Alpha", categories=("Category:Two",))

    def handler(params):
        if "clcontinue" not in params:
            payload = _page_response(first)
            payload["continue"] = {"continue": "||", "clcontinue": "1|Two"}
            return payload
        return _page_response(second)

    client = FakeClient(handler)
    page = _fetch_page(client, _config(), selector="pageids", value="1")
    row = _page_to_row(page)

    assert row["id"] == "1"
    assert row["revision_id"] == 201
    assert row["categories"] == ["One", "Two"]
    assert "Alpha links to the beta page." in row["content"]
    assert len(client.calls) == 2


def test_initial_prefix_sync_paginates_and_records_preflight_cursor():
    pages = {"1": _page("1", "Guide:Alpha"), "2": _page("2", "Guide:Beta")}

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:00Z")
        if params.get("list") == "allpages":
            if "apcontinue" not in params:
                return {
                    "continue": {"continue": "-||", "apcontinue": "Guide:Beta"},
                    "query": {"allpages": [{"pageid": 1, "ns": 0, "title": "Guide:Alpha"}]},
                }
            return {"batchcomplete": True, "query": {"allpages": [{"pageid": 2}]}}
        return _page_response(pages[str(params["pageids"])])

    client = FakeClient(handler)
    state = {}
    config = _config(page_titles=None, page_prefix="Guide:")

    rows = list(_iter_rows(client, config, state))

    assert [row["id"] for row in rows] == ["1", "2"]
    assert state == {
        _CURSOR_KEY: "2026-08-30T10:00:00Z",
        _SEEN_RCIDS_KEY: [],
    }
    assert client.calls[0]["curtimestamp"] == 1


def test_incremental_sync_fetches_only_new_in_scope_changes():
    changed = _page("1", "Alpha", content="Updated content")
    changes = [
        {
            "type": "edit",
            "ns": 0,
            "title": "Alpha",
            "pageid": 1,
            "rcid": 11,
            "timestamp": "2026-08-30T10:00:05Z",
        },
        {
            "type": "edit",
            "ns": 0,
            "title": "Outside",
            "pageid": 2,
            "rcid": 12,
            "timestamp": "2026-08-30T10:00:06Z",
        },
    ]

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:10Z")
        if params.get("list") == "recentchanges":
            return _recent_changes(changes)
        assert params["pageids"] == "1"
        return _page_response(changed)

    client = FakeClient(handler)
    state = {_CURSOR_KEY: "2026-08-30T10:00:00Z", _SEEN_RCIDS_KEY: []}

    rows = list(_iter_rows(client, _config(), state))

    assert [row["id"] for row in rows] == ["1"]
    assert rows[0]["content"] == "Updated content"
    assert state[_CURSOR_KEY] == "2026-08-30T10:00:10Z"
    assert state[_SEEN_RCIDS_KEY] == [11, 12]
    page_fetches = [call for call in client.calls if "pageids" in call]
    assert [call["pageids"] for call in page_fetches] == ["1"]


def test_delete_log_yields_hard_delete_without_fetching_page():
    deletion = {
        "type": "log",
        "ns": 0,
        "title": "Alpha",
        "pageid": 1,
        "rcid": 21,
        "timestamp": "2026-08-30T10:00:05Z",
        "logtype": "delete",
        "logaction": "delete",
        "logparams": {},
    }

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:10Z")
        return _recent_changes([deletion])

    client = FakeClient(handler)
    state = {_CURSOR_KEY: "2026-08-30T10:00:00Z", _SEEN_RCIDS_KEY: []}

    rows = list(_iter_rows(client, _config(), state))

    assert rows == [{"id": "1", "_deleted": True}]
    assert not any("pageids" in call for call in client.calls)


def test_overlap_skips_seen_event_but_processes_late_event():
    changes = [
        {
            "type": "edit",
            "ns": 0,
            "title": "Alpha",
            "pageid": 1,
            "rcid": 31,
            "timestamp": "2026-08-30T10:00:08Z",
        },
        {
            "type": "edit",
            "ns": 0,
            "title": "Beta",
            "pageid": 2,
            "rcid": 32,
            "timestamp": "2026-08-30T10:00:09Z",
        },
    ]

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:20Z")
        if params.get("list") == "recentchanges":
            assert params["rcstart"] == "2026-08-30T10:00:00Z"
            return _recent_changes(changes)
        assert params["pageids"] == "2"
        return _page_response(_page("2", "Beta", content="Late update"))

    client = FakeClient(handler)
    state = {_CURSOR_KEY: "2026-08-30T10:00:10Z", _SEEN_RCIDS_KEY: [31]}
    config = _config(page_titles=["Alpha", "Beta"])

    rows = list(_iter_rows(client, config, state))

    assert [row["id"] for row in rows] == ["2"]
    assert rows[0]["content"] == "Late update"
    assert state[_SEEN_RCIDS_KEY] == []


def test_move_out_of_prefix_emits_tombstone():
    move = {
        "type": "log",
        "ns": 0,
        "title": "Guide:Old",
        "pageid": 7,
        "rcid": 41,
        "timestamp": "2026-08-30T10:00:05Z",
        "logtype": "move",
        "logaction": "move",
        "logparams": {"target_ns": 0, "target_title": "Archive:Old"},
    }

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:10Z")
        return _recent_changes([move])

    state = {_CURSOR_KEY: "2026-08-30T10:00:00Z", _SEEN_RCIDS_KEY: []}
    rows = list(
        _iter_rows(
            FakeClient(handler),
            _config(page_titles=None, page_prefix="Guide:"),
            state,
        )
    )

    assert rows == [{"id": "7", "_deleted": True}]


def test_failed_page_fetch_does_not_advance_cursor():
    change = {
        "type": "edit",
        "ns": 0,
        "title": "Alpha",
        "pageid": 1,
        "rcid": 51,
        "timestamp": "2026-08-30T10:00:05Z",
    }

    class BrokenClient(FakeClient):
        def get(self, url, params, timeout):
            if "pageids" in params:
                raise ValueError("simulated fetch failure")
            return super().get(url, params, timeout)

    def handler(params):
        if params.get("curtimestamp") == 1:
            return _server_time("2026-08-30T10:00:10Z")
        return _recent_changes([change])

    state = {_CURSOR_KEY: "2026-08-30T10:00:00Z", _SEEN_RCIDS_KEY: []}
    original_state = deepcopy(state)

    with pytest.raises(RuntimeError, match="MediaWiki request failed"):
        list(_iter_rows(BrokenClient(handler), _config(), state))

    assert state == original_state


def test_config_requires_a_bounded_selector():
    with pytest.raises(ValueError, match="Select pages"):
        _config(page_titles=None, page_prefix=None)


def test_source_declares_document_mode_and_hard_delete_column():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_mediawiki import mediawiki_source

    resource = mediawiki_source(api_url=API_URL, page_titles=["Alpha"], client=FakeClient(None))

    assert document_source_tag(resource) == "mediawiki"
    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")

    assert resource.name == "mediawiki_pages"
    assert write_disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
